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
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.durations
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.instances
import subroutine.domain.schedule
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.domain.tags
import subroutine.errors

#: What separates a field from the operator applied to it. §9.6's spelling.
SEPARATOR = "."

#: Filters as somebody wrote them: **pairs in order, never a mapping** — `SR#2302`.
#:
#: **Because a name may legitimately be written twice and a mapping cannot hold it twice.**
#: ``tag.eq=ops tag.eq=web`` is an intersection — `#1801` §9 says so, and
#: :func:`subroutine.api.filters.Reader` reads ``request.query_params.multi_items()``, so the
#: instance has answered that question correctly for as long as the grammar has existed. It was
#: the *clients* that could not ask it: ``filters`` was a mapping in six signatures, so
#: ``--filter tag.eq=ops --filter tag.eq=web`` kept the last one and said nothing.
#:
#: A client unable to ask something its own API answers is the divergence ``test_reach`` and
#: ``tests/test_transport_equivalence.py`` exist to catch, and this is the shape it takes when
#: the gap is in a *type* rather than in a missing method.
Terms: typing.TypeAlias = typing.Sequence[tuple[str, str]]


#: The comparison operators, and what each does in SQL.
#:
#: **Comparison only, deliberately.** §9.2 also specifies `contains`, `startswith`, `in`, `any`
#: and more; each is a new way to write a query the database cannot serve from an index, and
#: none is needed by the questions this was built for. Decision `#817` records that the rest of
#: §9 arrives when something needs it rather than in advance — a half-built grammar promises
#: more than it does, which is worse than a small one that is honest.
OPERATORS: dict[str, typing.Callable[[typing.Any, typing.Any], typing.Any]] = {
	"eq": lambda column, value: column == value,
	# **`ne` takes the unset rows with it** — `SR#2285`'s neighbour, `SR#2284`. A bare
	# ``column != value`` is NULL, and therefore false, for a column nobody has set — so
	# ``urgency.ne=3`` answered about the *ranked* work alone and said nothing about the rest.
	# Driven when it was filed: three tasks at urgency 3, 5 and unset, and only the 5 came back.
	#
	# **Both readings are legitimate and only one of them was expressible.** *Everything that
	# is not 3* needs an OR, which §9's grammar deliberately does not have; *the ranked ones
	# that are not 3* is now ``urgency.ne=3&urgency.is=set``, because two comparisons about one
	# field are ANDed. So this is the reading that makes the other one askable, rather than a
	# choice between two equally reachable answers.
	#
	# It is also what the registry's own words for this operator say — *not the ones I said
	# were two hours* — and §6.3a's argument that ranked, part-ranked and unranked are three
	# states, which is why ``is=unset`` exists at all.
	#
	# **Written for every column rather than only the nullable ones.** On a ``NOT NULL`` column
	# the second half is never true, so one definition covers both and there is no rule to keep
	# in step with a schema.
	"ne": lambda column, value: sqlalchemy.or_(column != value, column.is_(None)),
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


#: *Whose responsibility is this*, asked one hop through the assignee — `#848`.
#:
#: **The value names a person and the question is about a chain**, so it resolves to a *set*:
#: the person named, plus every live service account whose responsibility chain terminates at
#: them. ``domain.accountability.agents_answering_to`` is that walk and this gives it its first
#: caller — the read half of `#473`'s model, which has been enforced on every authenticated
#: request since M1 and surfaced nowhere.
#:
#: **``eq`` alone, and both of the others are refused for their own reason.** ``is`` would have
#: to mean *has an assignee at all*, which is exactly ``assignee.is=unset`` — one question with
#: two spellings, and the narrower one lying about its subject. ``in`` — *work answerable to
#: si or to oli* — is coherent and nothing has asked for it, which is `#1804`'s own test for
#: ``all`` and ``contains``; it is one word here when something does.
#:
#: **Not a second copy of the chain rule** (`#925`, `#1420`). The browser could walk it —
#: ``views.User`` carries ``responsible_user_id`` and ``GET /v1/users`` is unpaginated — and a
#: governance rule with two implementations is what that pair refuses. The walk stays in
#: :mod:`subroutine.domain.accountability` and this asks it.
ANSWERABLE = Kind(
	predicate=_no_predicate_of_its_own,
	expects="a username",
	operators=frozenset({"eq"}),
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


#: Which group compiles every field whose value is the name of an account — `#1804`.
#:
#: **A group of one field at a time, and that is what the mechanism is for.** ``assignee``,
#: ``claimed_by`` and ``created_by`` never compile together — they are separate questions about
#: separate columns — but each needs the session to turn a username into an id, and a kind's
#: predicate is handed a value and a clock. :data:`GROUPS` is where a field whose compilation
#: needs more than its value goes, which `#817` built for ``touched_at`` and which needed
#: nothing rewritten here.
#:
#: **Was ``WHO_HOLDS_IT``, renamed by `#1577`.** It carried ``assignee`` and ``claimed_by``,
#: which are both about *holding*, and ``created_by`` is not — so the old name would have been
#: one word covering two things, which is the hazard decision `#1267` §2 records and this
#: registry exists to keep out of the vocabulary. What the group is really about is that the
#: value is a username, and that is what it says now.
NAMES_AN_ACCOUNT = "account"

#: Which group compiles ``actor`` on the change feed and the journal — `#1829`, `#2178`.
#:
#: **Apart from :data:`NAMES_AN_ACCOUNT` because ``me`` is a different column here**, and that
#: is `#158`'s decision rather than this registry's: on a feed ``me`` is *this credential*,
#: because an agent holding a service-account token wants what **it** did rather than what the
#: person who issued the token did from a laptop an hour ago. Any other value is a username,
#: which is the same question one grain coarser (`#1120`).
#:
#: **So one field compiles to two columns depending on the value, and that is carried rather
#: than invented.** The flat ``?actor=`` has meant exactly this since `#158`; a dotted spelling
#: that disagreed with it would be `#2175`'s defect — one word meaning two things on one
#: endpoint depending on how it is written — introduced deliberately by the item that found it.
WHO_DID_IT = "actor"

#: What ``actor`` means when the caller means themselves — **this credential, not this user**.
#:
#: **`#158`'s decision, and it lives here rather than in `api/changes` since `#2178`.** An agent
#: holding a service-account token wants what *it* did, not what the person who issued the token
#: did from a laptop an hour ago. Any other value is a username, which is the same question one
#: grain coarser (`#1120`): the coarse grain is the only one useful about somebody else, because
#: nobody knows another credential's id, and the fine one is the only one useful about yourself,
#: because your account may hold several.
#:
#: **In the domain because the registry compiles it and the endpoint compares against it**, and
#: a word two modules must agree about is a word one of them should own — §13.7's rule and
#: `#501`'s, which is why `ordering.PROJECT_FIELDS` moved.
MY_OWN_CREDENTIAL = "me"

#: Which group compiles ``tag``, whose predicate is a subquery over a join table — `#1804`.
TAGGED = "tagged"

#: Which group compiles ``status`` and ``type``, the two names drawn from a workspace's own
#: curated vocabulary — `#1829`.
#:
#: **One group for both because they resolve identically and refuse identically.** Each turns a
#: *key* into the id of a row in a per-workspace table, so each needs the session and exactly
#: one workspace, and each has to refuse an unknown key by listing what the workspace really
#: has. They never compile together — they are separate columns — which is what :data:`GROUPS`
#: is for and is the same shape :data:`NAMES_AN_ACCOUNT` has carried for ``assignee`` and
#: ``claimed_by`` since `#1804`.
#:
#: **The resolvers belong to the entity and are reached late.** ``domain.tasks`` imports
#: ``domain.ordering``, which reads this module's registries at module scope, so importing it
#: here at the top would be a cycle — the one the :class:`Property` docstring records. The
#: house style's nested-import exception is what :func:`_vocabulary_key` uses.
FROM_THE_VOCABULARY = "vocabulary"

#: Which group compiles ``project``, the one filter that resolves an *address* — `#1829`.
#:
#: **Alone rather than beside the vocabulary keys**, because what it needs from :class:`Where`
#: is different in kind: a status key is resolved against a table, and a project is resolved
#: against what this *caller* may see, which needs the principal and the workspace object.
IN_PROJECT = "in_project"

#: Which group compiles ``parent`` and ``under``, the two questions a tree is asked — `#1829`,
#: decided on `#2180`.
#:
#: **Two fields rather than one and a modifier**, Simon's decision of 2026-09-07. ``?parent=7``
#: is *directly under #7* and ``?parent=7&subtree=true`` is *anywhere below it* — one parameter
#: answering two questions, which a :class:`Property` cannot express. A boolean that modifies
#: another field is not a field, so keeping ``subtree`` would have left the second question
#: outside the registry: the search line could say *direct children* and could not say
#: *everything below*, which is `#2174`'s own complaint recreated inside the work meant to fix
#: it, and `#1801` §4's rule stated outright.
#:
#: **Spelled ``under`` rather than ``ancestor``, and the reason changed while the item waited.**
#: ``ancestor.eq=7`` names the relation from the row's point of view exactly as ``parent`` does,
#: which was the recommendation when `#2180` was filed. `#1806` shipped in between and made a
#: filter something people *type*, so how a name reads in a written line went from minor to
#: primary: ``under:7 deploy`` is immediately clear and ``ancestor:7 deploy`` needs a beat.
#: ``answers_to`` had already broken the all-nouns convention, so this is not the odd one out.
IN_THE_TREE = "tree"

#: The two fields that group compiles, spelled once so the predicate can tell them apart.
PARENT = "parent"
UNDER = "under"

#: The two field names that group resolves, spelled once so the predicate can tell them apart.
#:
#: **Named here rather than compared as literals**, because the predicate branches on which of
#: the two it is holding and a typo in that branch would resolve a status key against the type
#: table — which answers *there is no type called 'open'* about a field the caller spelled
#: correctly.
STATUS = "status"
TYPE = "type"
PROJECT = "project"

#: Which group compiles ``answers_to``, which resolves a username to the people it stands for
#: — `#848`. Alone, like a reference: what it needs from :class:`Where` is the session.
ANSWERABLE_TO = "answerable"

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
		group=NAMES_AN_ACCOUNT,
		because="ordering by an account id means nothing; ordering by who has what is `#1805`.",
	),
	# **What this compares against is not this column** — see :data:`LEASED` and `SR#2299`. A
	# claim is a lease, so an expired one reads as nobody holding it, and the swap is made
	# where every operator inherits it rather than per comparison.
	"claimed_by": Property(
		column=subroutine.db.models.work.Task.claimed_by_id,
		kind=REFERENCE,
		group=NAMES_AN_ACCOUNT,
		because="ordering by an account id means nothing — `claimed_at` is the sort that "
		"answers *taken longest ago*, and it is orderable.",
	),
	# **What did I file, against what did my agent file** — `#1577`, Simon's question (a) of
	# 2026-08-29. Reported on every row and in `selectable`, and askable by nothing: the
	# `test_api_writability` family with the direction reversed, where a caller reads a field
	# on every row and cannot ask for the rows carrying it.
	#
	# **`touched_by` is the nearest thing that worked and answers a different question.** It
	# reads the event feed, so it means *worked on*: driven on this instance, three items came
	# back for both accounts because both had touched them. That is right for what it is and is
	# an over-approximation of *created*.
	#
	# **`is` comes free and means something real here**, because the column is nullable:
	# `created_by.is=unset` is what the system wrote during setup, before any user existed.
	"created_by": Property(
		column=subroutine.db.models.work.Task.created_by,
		kind=REFERENCE,
		group=NAMES_AN_ACCOUNT,
		because=(
			"ordering by an account id means nothing, and *whose backlog is oldest* is "
			"`created_at`, which is orderable. Grouping by it is `#1803`'s third capability "
			"and wants a bounded axis, which an account list is not."
		),
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
	# **The two keys a workspace curates, `#1829`.** Both were flat parameters and nothing
	# else, so `status.in=open,in_progress` and `type.in=bug,spike` — the questions a planner
	# actually asks — had no spelling at all, while `?status=open` had one that could name only
	# a single value.
	#
	# **Not orderable, and the reason is the same for both**: a key is a word, so ordering by
	# it is alphabetical, and a status has `position` for the order somebody actually meant.
	# `#1805` is where that would be argued if anybody wanted it.
	STATUS: Property(
		column=subroutine.db.models.work.Task.status_id,
		kind=REFERENCE,
		group=FROM_THE_VOCABULARY,
		because=(
			"a status key sorts alphabetically, which is never the order somebody means — "
			"`status.position` is the workspace's own and `status_category` is the axis a "
			"board groups on."
		),
	),
	# **The third of `#1829`'s four**, and the one that needed `Where` widened: a project is
	# resolved against what this *caller* may see, so a key naming a private project somebody
	# is not a member of is **not found** rather than answered with an empty page.
	PROJECT: Property(
		column=subroutine.db.models.work.Task.project_id,
		kind=REFERENCE,
		group=IN_PROJECT,
		because=(
			"a project id sorts by nothing anybody means, and *group by project* is the "
			"question underneath — which wants a bounded axis and is `#1803`'s third "
			"capability rather than an ordering."
		),
	),
	TYPE: Property(
		column=subroutine.db.models.work.Task.type_id,
		kind=REFERENCE,
		group=FROM_THE_VOCABULARY,
		because="a type key sorts alphabetically, which puts `bug` above `spike` and means "
		"nothing. Nothing has asked to order by it.",
	),
	# **The two questions a tree is asked, settled on `#2180`** — `#1829`'s fourth and last.
	# This entry took `is` alone until 2026-09-07, because `?parent=` carried `subtree` with it
	# and one parameter answering two questions is not a field. It is two fields now.
	PARENT: Property(
		column=subroutine.db.models.work.Task.parent_task_id,
		kind=REFERENCE,
		group=IN_THE_TREE,
		because="ordering by a parent id means nothing, and *what is under what* is a shape "
		"rather than a sort — `#1790`'s grouping is where a tree would be drawn.",
	),
	# **Its column is the item's own identity, and that is what refuses `is`** — the idiom
	# `tag` established for the same reason. The predicate walks `path` and never compares this
	# column, so it is here for `_allowed` to read: `Task.id` is `NOT NULL`, so `under.is` is
	# refused without anybody saying so.
	#
	# **Which matters, because `under.is=unset` would be `parent.is=unset`.** Both would compile
	# to *has no parent* — one question with two spellings, on a pair of fields added in the
	# same commit. Written with `parent_task_id` here first, and the operator was offered.
	UNDER: Property(
		column=subroutine.db.models.work.Task.id,
		kind=REFERENCE,
		group=IN_THE_TREE,
		because="the same reason `parent` gives, and there is no one value to sort a subtree "
		"by at all.",
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
	# **The read half of `#473`'s model, which had no question at all** — `#848`. Handing work
	# *to* an agent has been built since M1 and the accountability chain is walked on every
	# authenticated request; asking *what came of it* reached nothing. `answers_to.eq=si` is si's
	# work and si's agents' work, in one request rather than a roster fetch and a join done by
	# whichever client wanted it.
	#
	# **Declared beside `assignee` rather than inside it**, because it is a different question
	# about the same column: `assignee` compares a value the caller names, and this resolves a
	# relation the instance holds. One field answering both would need an operator meaning
	# *and everybody who answers to them*, which is a rule hiding in a comparison.
	"answers_to": Property(
		column=subroutine.db.models.work.Task.assignee_id,
		kind=ANSWERABLE,
		group=ANSWERABLE_TO,
		because=(
			"ordering by an account id means nothing, and this names a set of them rather than "
			"one — there is no single value on the row to sort by. *Whose work is oldest* is "
			"`claimed_at` and `created_at`, both of which are orderable."
		),
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
	# **The same question on the other entity** — `#1577`. `GET /v1/documents` did not accept
	# `created_by` either, so this was missing on every surface at once.
	"created_by": Property(
		column=subroutine.db.models.work.Document.created_by,
		kind=REFERENCE,
		group=NAMES_AN_ACCOUNT,
		because="the task entry's reason, unchanged.",
	),
	# **The entry a task has carried since `#1804`, and the mechanism was already generic** —
	# `#2175`. `_tagged` reads the join table out of `tags.JOINS`, which has held `Document`
	# since `#1319`, so this is a declaration rather than an implementation.
	#
	# **It was missed for the reason `#1803` exists to remove.** The fields to carry across
	# when the registry was built were *remembered* rather than derived, so `?tag=` went on
	# working here while `tag.eq` was refused by name — one word meaning two things depending
	# on how it was spelled, on one endpoint. Reported by an agent that had learned the dotted
	# grammar on tasks and had no way to predict that documents did not carry it.
	"tag": Property(
		column=subroutine.db.models.work.Document.id,
		kind=REFERENCE,
		group=TAGGED,
		because=(
			"a row carries several tags, so there is no one value to sort it by — the task "
			"entry's reason, unchanged. `tag.is` is absent for its second reason too: the "
			"column is the item's own identity, so `_allowed` refuses it, and *has no tags at "
			"all* is a `NOT EXISTS` over the join table rather than a null column."
		),
	),
	# **A document is grouped on the same axis and its keys are its own** (`#1790`). Four
	# categories a *document* has, which are not a task's four — `db.mixins` keeps them apart
	# and this is where the two registries stop agreeing by accident.
	# **The same two keys, on the other entity — `#1829`.** A document's vocabulary is its own:
	# `entity_type` scopes the table, so a document's `current` and a task's `done` are
	# different rows and `documents.status_for` is the resolver that knows it.
	STATUS: Property(
		column=subroutine.db.models.work.Document.status_id,
		kind=REFERENCE,
		group=FROM_THE_VOCABULARY,
		because="the task entry's reason, unchanged — a key sorts alphabetically and "
		"`position` is the order the workspace meant.",
	),
	# **A document can be filed under another one, and nothing could ask** — `#2173`, Simon's
	# decision of 2026-09-07 from a board full of instrument specs with nothing to collapse
	# them behind. `Document.parent_id` has been built, indexed and reported since `#1534`;
	# `POST /v1/documents/{ref}/move` has set it since `#294`. No listing could read it back.
	#
	# **The task pair's spelling, unchanged** — `#2180`. A document growing a second vocabulary
	# for one relation is what `#1547` exists to refuse, and it would have been easy here:
	# *section* is the word this module's own docstrings reach for.
	PARENT: Property(
		column=subroutine.db.models.work.Document.parent_id,
		kind=REFERENCE,
		group=IN_THE_TREE,
		because="the task entry's reason, unchanged — ordering by a parent id means nothing.",
	),
	UNDER: Property(
		column=subroutine.db.models.work.Document.id,
		kind=REFERENCE,
		group=IN_THE_TREE,
		because="the task entry's reason, unchanged, including why the column is the item's "
		"own identity: `NOT NULL`, so `_allowed` refuses `is` and *has no parent* keeps its "
		"one spelling.",
	),
	PROJECT: Property(
		column=subroutine.db.models.work.Document.project_id,
		kind=REFERENCE,
		group=IN_PROJECT,
		because="the task entry's reason, unchanged.",
	),
	TYPE: Property(
		column=subroutine.db.models.work.Document.type_id,
		kind=REFERENCE,
		group=FROM_THE_VOCABULARY,
		because="the task entry's reason, unchanged.",
	),
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
#: **Two fields, and this said one until `#2178`.** ``actor_user_id`` is a column here too and
#: ``?actor=`` has compared it since M1; what kept it out of the registry was the belief that a
#: `REFERENCE` would lose ``me``, which on a feed is *this credential* where everywhere else it
#: is *this account*. It does not: :data:`WHO_DID_IT` compiles the same two readings the flat
#: parameter has always had, so the two spellings cannot answer about different rows.
#:
#: **What genuinely cannot be declared here is everything the event is *about*** — which
#: project, which item. Reaching those means a join this registry has no way to express, and
#: that is filed separately rather than bent into a `Filterable`.
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
	# **Who did it** — `#2178`. `#1806`'s line cannot ask about a feed today, but `#1382`
	# PHASE 2's live digest wants a *set* of actors, and `in` falls out of a registry entry for
	# nothing — which is an argument for declaring it before that rather than after.
	#
	# **`is` comes free and means the system wrote it**: `actor_user_id` is nullable, so
	# `actor.is=unset` is everything no account did.
	WHO_DID_IT: Property(
		column=subroutine.db.models.activity.Event.actor_user_id,
		kind=REFERENCE,
		group=WHO_DID_IT,
		because=(
			"a feed always runs forwards and the caller chooses only which end to start "
			"from — `newest` — so this listing offers no ordering at all rather than one "
			"that would contradict the cursor."
		),
	),
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


def parsed (given: typing.Iterable[str]) -> Terms:
	"""Read ``field.operator=value`` as somebody types it, refusing anything shapeless.

	The form a *terminal* takes, since a query string's separator is not something to type by
	hand. Here rather than in the CLI so that the refusal is one sentence wherever it is met,
	and so a second surface taking the same spelling cannot invent a second one.

	**The value is split on the first ``=`` only**, because a date expression may legitimately
	contain one later and losing the tail would produce a filter that parses and means
	something else.

	**Pairs in order, and a repeated name is kept** — `SR#2302`. This returned a mapping, so
	``--filter tag.eq=ops --filter tag.eq=web`` arrived as one comparison and the other was
	gone before any client saw it. The written line has ANDed a repeat since `#1806` and so has
	the API; only the flag disagreed, and silently.
	"""

	found = []

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

		found.append((name.strip(), value.strip()))

	# **Before a client is chosen, which is what makes both transports agree** (`SR#1626`).
	# The terminal's ``--filter`` is only ever filters, so a flat name here is nobody's — and
	# the local client and the HTTP client would otherwise drop it in two different places for
	# two different reasons.
	refuse_names_that_are_not_filters(name for name, _ in found)

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

	#: Who is asking. ``None`` means nobody was offered, so ``me`` is an ordinary username and
	#: will not resolve — the sentinel is a courtesy to somebody asking about themselves (`#518`),
	#: never a way to ask about somebody whose name you do not know.
	#:
	#: **A principal rather than the user, since `#1829`.** It was a ``User`` and every caller
	#: set it from ``actor.user``, so the second field a project filter needs — a principal, to
	#: refuse a project the caller cannot see *by name* rather than answering an empty page —
	#: would have been a second variable answering the same question. One of them, and the user
	#: is read off it where a username is what is wanted.
	principal: subroutine.domain.authentication.Principal | None = None

	#: The one workspace this listing reads, for a filter that resolves an address rather than
	#: a bare name — `#1829`.
	#:
	#: **Beside ``workspace_ids`` rather than instead of it**, because they answer different
	#: questions: the sequence is what a subquery narrows by and may hold several, and this is
	#: the object ``selection.project`` needs and exists only when there is exactly one. A feed
	#: spanning workspaces has ids and no workspace, which is the state that makes a filter
	#: needing one refuse rather than guess.
	workspace: subroutine.db.models.identity.Workspace | None = None

	#: Which workspaces the listing is already narrowed to, so a subquery over another table
	#: can reach an index keyed on one. **Not a visibility control** — :func:`_touched` explains
	#: why the join decision `#817` called for turned out to narrow nothing.
	workspace_ids: typing.Sequence[uuid.UUID] = ()


#: Fields whose value is only true while a lease is live, and the column that says when it
#: ran out — `SR#2299`.
#:
#: **A claim is a lease and an expired one is ignored** (`#726`, §10.7 invariant 10): a worker
#: that dies must not strand the work, so nothing is cleaned up eagerly and every reader tests
#: the clock. Four already did — the ``claimed_by`` route parameter, ``readiness.held`` in SQL,
#: ``claims.held_by`` on a row and ``views.holder`` on a rendered item — and the *filter*
#: matched the raw column, so one name meant two things. Measured on the served instance the
#: day it was filed: ``--claimed-by claude-super`` answered *nothing on your list* while
#: ``--filter claimed_by.eq=claude-super`` answered with two items whose leases had run out
#: three weeks and one day earlier.
#:
#: **``claimed_at`` is deliberately not here.** It records *when* a claim was taken, which is a
#: fact about something that happened and stays true; and there is no flag beside it saying
#: something else, so nothing disagrees.
LEASED: dict[str, typing.Any] = {
	"claimed_by": subroutine.db.models.work.Task.claim_expires_at,
}


def _while_the_lease_lasts (
	comparison: Comparison, *, now: datetime.datetime
) -> Comparison:
	"""Read a leased field as unset once its lease has run out — `SR#2299`.

	**The column is swapped rather than the predicate qualified**, and that is what makes this
	one rule instead of four. ``eq``, ``in``, ``is=set`` and ``is=unset`` all have to move
	together — an expired claim is *nobody holding it*, so ``claimed_by.is=unset`` must reach
	it while ``claimed_by.eq=si`` must not — and a clause ANDed onto whatever a comparison
	compiled to would be right for three of those and exactly wrong for the fourth.

	So what the caller compares against is *what the column effectively holds*, which is
	§10.7 invariant 10 written as SQL: an expired claim is treated as absent.

	``now`` is the request's own instant rather than the database's clock, like every other
	expression in :class:`Where` — §9.3's rule, so two comparisons in one request cannot land
	on different sides of a lease that expired between them.
	"""

	expiry = LEASED.get(comparison.field)

	if expiry is None:
		return comparison

	return comparison._replace(
		against=comparison.against._replace(
			column=sqlalchemy.case((expiry > now, comparison.against.column))
		)
	)


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

	for asked in comparisons:
		# **Before anything else, because it decides what is being compared** — `SR#2299`. A
		# field whose value is a lease reads as unset once that lease has run out, and doing it
		# here rather than in each compiler is what keeps the four operators agreeing.
		comparison = _while_the_lease_lasts(asked, now=where.now)

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
		where.session, comparison.value, caller=_the_user(where)
	).id


def values_for (comparisons: typing.Iterable[Comparison], field: str) -> list[str]:
	"""Return every value these comparisons name for one field, with :data:`IN` split out.

	**The companion to :func:`about`, which answers *whether* where this answers *what*** —
	`#1829`. Some rules need the value and not only the presence: naming a finished status
	decides whether the listing reaches finished work at all (`#1032`), and that has to be
	settled before the statement these predicates are added to is built.

	**Split the same way the predicate splits it**, through the one function, so a rule read
	off a filter and the filter itself can never disagree about what ``status.in=open,done``
	named.
	"""

	found = []

	for comparison in comparisons:
		if comparison.field == field:
			found.extend(_values(comparison))

	return found


def values_named (
	parameters: typing.Iterable[tuple[str, str]], *, entity: str, field: str
) -> list[str]:
	"""Return the values one field was given, reading the dotted names as written.

	**For a caller holding raw parameters rather than resolved comparisons** — the local
	client, which hands its filters straight to :func:`asked`. Resolving them here rather than
	partitioning the names by hand is what makes an alias work: `#1017`'s trap is a rule that
	compares ``due_after`` against ``due_at`` and answers no about a filter that was applied.
	"""

	return values_for(understood(parameters, entity=entity), field)


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


def _who_did_it (comparisons: list[Comparison], where: Where) -> typing.Any:
	"""Compile ``actor`` on a feed — a credential when ``me``, an account otherwise, `#2178`.

	**A caller with no token matches nothing rather than everything.** A session-authenticated
	principal has no ``actor_token_id`` on anything it wrote, so comparing against a null token
	would quietly widen the filter to every system-written row — and the belief being tested is
	precisely *these are the things I did*. ``events.feed`` says the same about the flat
	spelling, and this is that sentence kept rather than restated.

	**``in`` may mix the two**, which is why the clauses are ORed: ``actor.in=me,si`` is *what I
	did through this credential, or anything that account did*, and both are questions about
	who acted.
	"""

	if where.session is None:
		raise AssertionError("an actor needs a session to resolve a name")

	model = subroutine.db.models.activity.Event
	token = None if where.principal is None else where.principal.token
	narrowing = []

	for comparison in comparisons:
		clauses = []

		for value in _values(comparison):
			if value == MY_OWN_CREDENTIAL:
				clauses.append(
					sqlalchemy.false()
					if token is None
					else model.actor_token_id == token.id
				)

				continue

			clauses.append(
				model.actor_user_id
				== subroutine.domain.selection.user(
					where.session, value, caller=_the_user(where)
				).id
			)

		narrowing.append(sqlalchemy.or_(*clauses))

	return sqlalchemy.and_(*narrowing)


def _an_account (comparisons: list[Comparison], where: Where) -> typing.Any:
	"""Compile a field whose value names an account — ``assignee``, ``claimed_by``,
	``created_by`` — `#1804`, `#1577`.

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
				where.session, value, caller=_the_user(where)
			).id
			for value in _values(comparison)
		]

		narrowing.append(
			column.in_(found) if comparison.operator == IN else column == found[0]
		)

	return sqlalchemy.and_(*narrowing)


#: How a vocabulary key is turned into a row: the session, the workspace, and the key itself.
#:
#: **The shape both entities' resolvers already have**, so declaring it costs nothing and makes
#: the pair below type-checked rather than a tuple of unknowns.
_Resolver: typing.TypeAlias = typing.Callable[
	[sqlalchemy.orm.Session, uuid.UUID, str], typing.Any
]


def _the_user (where: Where) -> subroutine.db.models.identity.User | None:
	"""Return the account ``me`` stands for, or ``None`` when nobody was offered.

	One line, and it exists so the three filters that resolve a username read the user off the
	principal in one place rather than three — `#1829`, where :attr:`Where.caller` became
	:attr:`Where.principal` so a project filter would not need a second field saying who is
	asking.
	"""

	return None if where.principal is None else where.principal.user


#: How a ref is turned into the item it names: the session, who is asking, the workspace, and
#: the ref itself. Both entities' resolvers already have this shape.
_Finder: typing.TypeAlias = typing.Callable[
	[
		sqlalchemy.orm.Session,
		subroutine.domain.authentication.Principal,
		subroutine.db.models.identity.Workspace,
		str,
	],
	typing.Any,
]


def _in_the_tree (comparisons: list[Comparison], where: Where) -> typing.Any:
	"""Compile ``parent`` and ``under`` — one level, and everything below it, `#1829`.

	**Two fields, one predicate, because they resolve identically and differ only in how far
	down they look.** Each turns a ref into a task through
	:func:`subroutine.domain.selection.task`, which refuses one the caller cannot see **by
	name** — an unresolved ref answered with an empty listing would say the subtree is empty,
	which is a different and false claim (§7.3a).

	**``under`` excludes the item itself**, which is what the flat ``subtree=true`` has always
	done: *everything below #7* is not *#7 and everything below it*, and a reader asking what a
	milestone contains does not want the milestone in the answer.

	**Through :func:`subroutine.domain.hierarchy.subtree`**, so the ``LIKE``-not-a-range
	decision is made once — a half-open range over ``path`` silently drops descendants under a
	non-byte-wise collation, correctly on SQLite and wrongly on PostgreSQL.
	"""

	if where.session is None or where.principal is None or where.workspace is None:
		raise subroutine.errors.ValidationError(
			"A parent can only be asked about inside one workspace.",
			errors=[
				subroutine.errors.FieldError(
					field=comparisons[0].field,
					code="invalid_field_value",
					message="A ref is numbered within a workspace, and this listing reads "
					"more than one.",
					hint="Ask one workspace at a time — 'workspace_id' narrows a listing.",
				)
			],
		)

	owner = typing.cast(typing.Any, comparisons[0].against.column).parent.class_
	# **The entity decides which resolver, read off the column** — `_tagged`'s shape, and it
	# matters here for a reason that shape usually does not carry: one counter numbers tasks
	# and documents together (§6.2), so `#7` names exactly one of them and asking the wrong
	# module would refuse a ref that exists. Both resolvers say so by name when it happens.
	resolve: _Finder = {
		subroutine.db.models.work.Task: subroutine.domain.selection.task,
		subroutine.db.models.work.Document: subroutine.domain.selection.document,
	}[owner]
	narrowing = []

	for comparison in comparisons:
		clauses = []

		for value in _values(comparison):
			above = resolve(where.session, where.principal, where.workspace, value)

			clauses.append(
				sqlalchemy.and_(
					subroutine.domain.hierarchy.subtree(owner, above),
					owner.id != above.id,
				)
				if comparison.field == UNDER
				else comparison.against.column == above.id
			)

		narrowing.append(sqlalchemy.or_(*clauses))

	return sqlalchemy.and_(*narrowing)


def _in_project (comparisons: list[Comparison], where: Where) -> typing.Any:
	"""Compile ``project`` — one project *and everything filed underneath it*, `#1829`.

	**Through :func:`subroutine.domain.selection.project` and
	:func:`subroutine.domain.scoping.within_project`, the two the flat parameter already
	uses.** The first refuses a project this caller cannot see **by name**, where comparing an
	unresolved key would answer an empty listing — §7.3a's distinction, and the reason this
	filter needs a principal rather than a user. The second is `#320`'s rule: a named project
	means that area of work, so a parent that answered for none of its contents would make the
	tree decorative.

	**A predicate over the *project's* path rather than the item's column**, which is why this
	works unchanged on both entities: `readable_tasks` and `readable_documents` both join
	``project``, and the declaration's column is there for :func:`_allowed` to read — both
	``project_id`` columns are ``NOT NULL``, so ``is`` is refused without anybody saying so.

	**``in`` is *any of these*, so the subtrees are ORed**; two separate comparisons about
	``project`` are still ANDed, which is a caller asking for the intersection of two areas and
	is `#1801` §9's stated shape for the query string.
	"""

	if where.session is None or where.principal is None or where.workspace is None:
		raise subroutine.errors.ValidationError(
			"A project can only be asked about inside one workspace.",
			errors=[
				subroutine.errors.FieldError(
					field=comparisons[0].field,
					code="invalid_field_value",
					message="A project is addressed within a workspace, and this listing reads "
					"more than one.",
					hint="Ask one workspace at a time — 'workspace_id' narrows a listing.",
				)
			],
		)

	narrowing = []

	for comparison in comparisons:
		chosen = [
			subroutine.domain.scoping.within_project(
				subroutine.domain.selection.project(
					where.session, where.principal, where.workspace, value
				)
			)
			for value in _values(comparison)
		]

		narrowing.append(sqlalchemy.or_(*chosen))

	return sqlalchemy.and_(*narrowing)


def _vocabulary_key (comparisons: list[Comparison], where: Where) -> typing.Any:
	"""Compile ``status`` and ``type`` — a key from the workspace's own vocabulary, `#1829`.

	**Through the same resolver the flat parameter uses**, which is the whole of why this is
	worth doing rather than comparing ids: ``tasks.status_for`` refuses an unknown key by
	*listing the ones this workspace has*, where a raw ``status_id`` comparison would answer an
	empty page and say nothing. A vocabulary is renameable and per workspace, so a caller who
	guesses needs to be told what to guess from.

	**The entity decides which resolver, read off the column** — the shape :func:`_tagged`
	established. A status is `entity_type` scoped, so a task's ``done`` and a document's
	``current`` are different rows in one table and asking the wrong module would silently
	compare against the wrong half of it.

	**Reached late, because ``domain.tasks`` imports ``domain.ordering``**, which reads this
	module's registries at module scope. The house style's nested-import exception covers
	exactly this, and a plain ``import subroutine.domain.tasks`` in a function body would bind
	``subroutine`` locally and shadow every other use of it here.
	"""

	from subroutine.domain import documents as for_documents
	from subroutine.domain import tasks as for_tasks

	if where.session is None:
		raise AssertionError("a vocabulary key needs a session to resolve a name")

	# **One workspace, refused rather than guessed at** — `_tagged`'s rule and for its reason:
	# a status key belongs to a workspace, so a listing reading several has no one table to
	# resolve against. The local client merging connections is what reaches this.
	if len(where.workspace_ids) != 1:
		raise subroutine.errors.ValidationError(
			"A status or a type can only be asked about inside one workspace.",
			errors=[
				subroutine.errors.FieldError(
					field=comparisons[0].field,
					code="invalid_field_value",
					message="A workspace curates its own statuses and types, and this reads "
					"several.",
					hint="Ask one workspace at a time — 'workspace_id' narrows a listing.",
				)
			],
		)

	owner = typing.cast(typing.Any, comparisons[0].against.column).parent.class_
	# **Typed, because a registry annotated loosely is where the next defect hides.** Only the
	# key is `Any` — it is a mapped class — and the pair is the two resolvers in a fixed order,
	# so swapping them is a type error rather than a status key looked up in the type table.
	resolvers: dict[typing.Any, tuple[_Resolver, _Resolver]] = {
		subroutine.db.models.work.Task: (for_tasks.status_for, for_tasks.item_type_for),
		subroutine.db.models.work.Document: (
			for_documents.status_for,
			for_documents.item_type_for,
		),
	}
	by_status, by_type = resolvers[owner]
	narrowing = []

	for comparison in comparisons:
		column = comparison.against.column
		resolve = by_status if comparison.field == STATUS else by_type
		found = [
			resolve(where.session, where.workspace_ids[0], value).id
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


def _answerable_to (comparisons: list[Comparison], where: Where) -> typing.Any:
	"""Compile *whose responsibility is this* — ``answers_to`` — `#848`.

	**One username resolved to a set of accounts**, by the same walk every authenticated request
	makes: the person named, plus every live service account answerable to them directly or
	through another. So ``answers_to.eq=si`` is *si's work and si's agents' work*, which is the
	question `#473`'s model made true and nothing could ask.

	**Resolved by `selection.user`, exactly as ``assignee`` is.** That is what makes this accept
	the same values the flat spelling accepts — a username or an id, ``me`` understood, refused
	by name rather than answered emptily — instead of being a second, narrower door onto one
	column.

	**The person is in their own set**, which is the whole point: `#518` shipped *assigned to
	me* and this is *mine and theirs*, so leaving the caller out would make it answer a question
	nobody asked and force two requests to ask the one they did.
	"""

	if where.session is None:
		raise AssertionError("answers_to needs a session to resolve a username")

	narrowing = []

	for comparison in comparisons:
		person = subroutine.domain.selection.user(
			where.session, comparison.value, caller=_the_user(where)
		)
		standing = [person.id] + [
			agent.id
			for agent in subroutine.domain.accountability.agents_answering_to(
				where.session, person
			)
		]

		narrowing.append(comparison.against.column.in_(standing))

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
	NAMES_AN_ACCOUNT: _an_account,
	WHO_DID_IT: _who_did_it,
	TAGGED: _tagged,
	FROM_THE_VOCABULARY: _vocabulary_key,
	IN_PROJECT: _in_project,
	IN_THE_TREE: _in_the_tree,
	ANSWERABLE_TO: _answerable_to,
}


def asked (
	parameters: typing.Iterable[tuple[str, str]],
	*,
	entity: str,
	now: datetime.datetime,
	timezone: str,
	session: sqlalchemy.orm.Session | None = None,
	principal: subroutine.domain.authentication.Principal | None = None,
	workspace_ids: typing.Sequence[uuid.UUID] = (),
	workspace: subroutine.db.models.identity.Workspace | None = None,
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
			principal=principal,
			workspace_ids=workspace_ids,
			workspace=workspace,
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
