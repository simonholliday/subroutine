"""Which fields a listing may be sorted by, and what each one means.

**Here rather than in the router, because both transports have to agree.** The vocabulary
started in ``api/tasks.py``, which the CLI's local client may not import — ``api.tasks``
imports FastAPI, and paying 0.3s of a 0.8s start to load a web framework in order to print a
to-do list is the cost ``subroutine serve``'s late imports exist to avoid. So a local listing
had no ordering at all and every client-side list was newest-first, which is the same
divergence S3-07 removed for the task *shape*, recreated for its order.

What stays in ``api/pagination`` is the part that genuinely belongs to HTTP: keyset cursors,
their signing, and the seek predicate. What is here is the answer to "what does
``?order=priority_score`` mean", which is a fact about the domain and identical over every
transport.
"""

import dataclasses
import datetime
import typing

import sqlalchemy
import sqlalchemy.orm
import sqlalchemy.orm.interfaces

import subroutine.db.fulltext
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.readiness
import subroutine.errors


@dataclasses.dataclass(frozen=True)
class Derived:
	"""A sort field computed from other columns rather than stored in one of its own.

	**Both halves are required, and that is the whole point of this class.** An ordering
	needs an expression the database can sort by; a *cursor* needs the same value read back
	off a loaded row, and a computed expression has a ``.key`` of ``None``.

	``priority_score`` was declared as a bare ``importance * urgency``, which orders perfectly
	and returned 500 for every result set larger than one page, because encoding a cursor read
	each sort value with ``getattr(row, key.column.key)``. See ``#46``.

	``carried_on`` names a :func:`sqlalchemy.orm.query_expression` attribute for the second
	half to arrive on, instead of being computed a second time in Python. Set it when the
	expression reads rows *other than* the one being sorted — where a Python copy is not
	merely duplication but impossible, since a loaded row knows nothing about its neighbours
	(`#569`). :func:`options` is then what puts the value there, and :meth:`carrying` is the
	only place that pairing is written down.
	"""

	expression: sqlalchemy.ColumnElement[typing.Any]
	read: typing.Callable[[typing.Any], typing.Any]
	carried_on: sqlalchemy.orm.InstrumentedAttribute[typing.Any] | None = None

	def carrying (self) -> sqlalchemy.orm.interfaces.ORMOption | None:
		"""Return the loader option that puts this field's value on each loaded row.

		``None`` where the field computes itself from the row it sorts, which needs no help.
		"""

		if self.carried_on is None:
			return None

		return sqlalchemy.orm.with_expression(self.carried_on, self.expression)


#: What a sortable map may hold: a column, or a computed field that knows how to read itself
#: back. Annotate those maps with this rather than with ``typing.Any``, which is what let
#: ``#46`` past the type checker.
Sortable = sqlalchemy.orm.InstrumentedAttribute[typing.Any] | Derived

#: What the three ranking states are worth to an ordering (SPEC.md §6.3a). **This decides how
#: ``?order=priority_score`` arranges items and deliberately does not change what
#: ``priority_score`` *is*** — the field a caller reads is still ``importance * urgency``,
#: null unless both are set. Two different things, and conflating them would put an ordering
#: concern into a published field.
#:
#: An item is in one of three states and, before 2026-07-30, an ordering could see only two:
#:
#: * **Ranked** — both axes set. Ordered among themselves by the product, 1 to 25.
#: * **Part-ranked** — one axis set. Ordered among themselves by whichever it is, 1 to 5.
#: * **Unranked** — neither. Null, and NULLS LAST in both directions puts it at the end.
#:
#: The defect this fixes: part-ranked and unranked both scored null, so "critically important,
#: urgency not yet judged" sorted *below* "explicitly judged trivial and not urgent" — the
#: person who said the most about an item was penalised for not finishing the sentence.
#:
#: **The claim being made is that part-ranked sits between ranked and unranked**, because
#: "assessed and incomplete" carries more information than "not assessed" and less than a
#: finished assessment. That is a judgement rather than a fact, it is Simon's decision of
#: 2026-07-30, and it is the thing to revisit if it ever feels wrong. Changing it means
#: changing these two constants *and* both functions below — which is what the equivalence
#: test in ``tests/test_api_tasks.py`` exists to enforce.
#:
#: The bands are separated by 100, comfortably more than the 25 a product can reach, so a band
#: can never be entered from below.
RANKED_BAND = 200
PART_RANKED_BAND = 100


def carried (row: typing.Any) -> int | None:
	"""Return the ordering value SQL computed for this row, for a cursor to carry.

	**There is no Python copy of the rule any more, and that is deliberate** (`#569`). §6.3a
	used to require two implementations — a SQL expression that orders the query and a Python
	function naming the row a cursor stopped at — and warned that a disagreement between them
	is a page boundary which skips or repeats rows. An ordering that reads *other* rows cannot
	have the second one at all: a loaded task does not know what it blocks. So the expression
	is the only copy and this reads its answer, put on the row by :func:`options`.

	**A missing value is raised rather than returned**, because the alternative is the exact
	corruption the two-copies rule existed to prevent. A query that sorts by this and forgets
	the loader option would otherwise hand the cursor ``None`` for every row and paginate
	nonsense, silently — where a task carrying either axis always has a rank.
	"""

	value: int | None = getattr(row, "rank", None)

	if value is not None:
		return value

	# Unranked is a real answer and sorts last (§6.3a); an *unloaded* rank is a bug here.
	if row.importance is None and row.urgency is None:
		return None

	raise subroutine.errors.InternalError(
		"This task's ordering value was never computed, so a page boundary cannot be named.",
		hint="The query sorted by a carried field without applying ordering.options().",
	)


#: What ``?order=`` calls the search ranking. Named rather than spelled at four call sites, so
#: the vocabulary a guard reads and the vocabulary a caller sends are one string.
RELEVANCE = "relevance"


def scored (row: typing.Any) -> float | None:
	"""Return the relevance SQL computed for this row, for a cursor to carry.

	:func:`carried`'s sibling, and separate rather than parameterised because the two differ in
	the half that matters: an unranked task legitimately has no priority, where **a row
	returned by a search always has a relevance**. So there is no "this is genuinely null"
	branch here — a missing value is always the loader option having been forgotten.
	"""

	value: float | None = getattr(row, RELEVANCE, None)

	if value is not None:
		return value

	raise subroutine.errors.InternalError(
		"This row's search ranking was never computed, so a page boundary cannot be named.",
		hint="The query sorted by relevance without applying ordering.options().",
	)


def searching (
	allowed: typing.Mapping[str, Sortable],
	*,
	terms: typing.Sequence[str],
	columns: typing.Sequence[typing.Any],
	carried_on: sqlalchemy.orm.InstrumentedAttribute[typing.Any],
	ref: typing.Any = None,
	numbered: int | None = None,
) -> dict[str, Sortable]:
	"""Return this vocabulary with ``relevance`` added, for one query.

	**A per-request entry, because the expression depends on the search**, which is why this is
	a function rather than a line in :data:`TASK_FIELDS`. Those maps are static and every other
	entry names a column; a ranking names a *question*, and the same listing sorted by the same
	name means something different for every caller.

	That is exactly what made this the piece both `#823` and `#867` were waiting on, and why
	neither built half of it: a sort value that cannot be read back off a loaded row is `#46`'s
	defect — ``priority_score`` shipped that way and returned **500 for every result set larger
	than one page**, invisible because the pagination tests only ever walked the default order.
	:class:`Derived` has carried its own reader since; this hands it one.

	**The caller's vocabulary is copied rather than mutated.** A module-level dict quietly
	gaining a per-request entry would leak one caller's search into the next one's ordering, and
	the symptom would be a page ordered by somebody else's question.
	"""

	return {
		**allowed,
		RELEVANCE: Derived(
			expression=subroutine.db.fulltext.rank(
				terms, *columns, ref=ref, numbered=numbered
			),
			read=scored,
			carried_on=carried_on,
		),
	}


#: What ``?order=`` calls the deferral band. There is only one direction worth having and it is
#: the plain one: ascending puts work that can be started first and work somebody has put off
#: last, which is Simon's decision of 2026-08-14 — *"deferred items appearing last. That way
#: they are not invisible, but neither are they confused with non-deferred items in lists."*
DEFERRED = "deferred"

#: The two bands, named rather than spelled, because the SQL that assigns one and the readers
#: that recognise one are in three different files. Separated by 1 rather than by 100: unlike
#: §6.3a's ranking there is nothing to add to them, so nothing can enter a band from below.
STARTABLE_BAND = 0
DEFERRED_BAND = 1


def parked (row: typing.Any) -> int:
	"""Return the deferral band SQL computed for this row, for a cursor to carry.

	:func:`carried`'s sibling and :func:`scored`'s, and strict for :func:`scored`'s reason: a
	band is assigned to *every* row a listing returns, so a missing one is always the loader
	option having been forgotten rather than a value the row genuinely lacks.
	"""

	value: int | None = getattr(row, "parked", None)

	if value is not None:
		return value

	raise subroutine.errors.InternalError(
		"This row's deferral band was never computed, so a page boundary cannot be named.",
		hint="The query sorted by deferral without applying ordering.options().",
	)


def never (row: typing.Any) -> int:
	"""Return the deferral band of a kind that has no start date: always the first one."""

	return STARTABLE_BAND


def put_off (item: typing.Any) -> int:
	"""Return the deferral band of a **rendered row**, for a client re-sorting a merged page.

	**A third reading of the clock, and the third is the one that has to be a copy.** The
	query's band is assigned by SQL against the request's instant; this runs in a client
	holding rows an instance has already sent, and has neither that instant nor a way to ask
	for it. Both spellings say the same thing — no start date is startable, and one that has
	passed is startable — which is the agreement ``readiness.undeferred`` and the two marking
	functions already keep.

	**Only while the defer is still hiding something**, which is why this is not simply
	``start_at is not None``: once the instant has passed the task behaves like any other, and
	sinking it would be sorting on a decision that has already taken effect.
	"""

	start = getattr(item, "start_at", None)

	if start is None:
		return STARTABLE_BAND

	return DEFERRED_BAND if start > datetime.datetime.now(datetime.UTC) else STARTABLE_BAND


#: What ``deferred`` means to a kind that has no start date at all. **A constant rather than an
#: absence, and that is the decision** (`#877`): §6.14 says a document is not scheduled, so it
#: can never be deferred — but a sort name only one half of a merged listing accepts makes
#: ``collectionsFor`` drop the other half (`#782`), and a list of items that silently held only
#: tasks would tell a reader who has learned that a number names an item that half the numbers
#: do not exist. So a document answers the question, with the answer *no*.
#:
#: **Sorted by a bind parameter rather than by the literal 0**, because PostgreSQL reads a bare
#: integer in ``ORDER BY`` as a column position. Measured on both backends rather than reasoned
#: about; :func:`sqlalchemy.literal` renders a parameter and both accept it.
UNDEFERRABLE = Derived(expression=sqlalchemy.literal(0, sqlalchemy.Integer), read=never)


def sinking (
	allowed: typing.Mapping[str, Sortable],
	*,
	model: type[typing.Any] | None = None,
	now: datetime.datetime | None = None,
) -> dict[str, Sortable]:
	"""Return this vocabulary with ``deferred`` added, for one request.

	**Per request rather than in the static maps, because the answer depends on the clock**, and
	:func:`searching` is the precedent: a vocabulary entry whose meaning changes between two
	calls cannot be a module constant. ``now`` is passed in for ``readiness.undeferred``'s
	reason — one request settles every relative comparison against a single instant, so the
	rows a listing *excludes* as deferred and the rows it *sinks* can never disagree.

	**Omit ``model`` for a kind that has no start date**, which is a document: it takes
	:data:`UNDEFERRABLE` and sorts in the first band always.

	**Adding the name changes no existing answer.** ``deferred`` is in neither default order, so
	a caller that does not ask for it is untouched — which is `readiness.DEFAULT_DEFERRAL`'s
	argument applied to the ordering it was written about. §6.5's *"default views hide it
	entirely"* is about views a person reads; an API listing is not one, and re-ordering every
	listing on their behalf would be the same imposition as re-filtering it.
	"""

	if model is None:
		return {**allowed, DEFERRED: UNDEFERRABLE}

	if now is None:
		raise ValueError("A deferral band needs the instant it is judged against.")

	return {
		**allowed,
		DEFERRED: Derived(
			expression=sqlalchemy.case(
				(subroutine.domain.readiness.undeferred(model, now=now), STARTABLE_BAND),
				else_=DEFERRED_BAND,
			),
			read=parked,
			carried_on=model.parked,
		),
	}


#: The SQL half of the same rule. A ``CASE`` cannot use a plain index, and neither could the
#: bare ``importance * urgency`` it replaces, so this costs nothing that was not already paid.
RANKING = sqlalchemy.case(
	(
		sqlalchemy.and_(
			subroutine.db.models.work.Task.importance.is_not(None),
			subroutine.db.models.work.Task.urgency.is_not(None),
		),
		RANKED_BAND
		+ subroutine.db.models.work.Task.importance * subroutine.db.models.work.Task.urgency,
	),
	(
		subroutine.db.models.work.Task.importance.is_not(None),
		PART_RANKED_BAND + subroutine.db.models.work.Task.importance,
	),
	(
		subroutine.db.models.work.Task.urgency.is_not(None),
		PART_RANKED_BAND + subroutine.db.models.work.Task.urgency,
	),
	else_=None,
)

#: What ``?order=`` accepts on a task listing, and the columns the names mean. Deliberately a
#: short list: every entry is a promise about an index, and a sort the database cannot serve
#: cheaply is worse than no sort at all.
TASK_FIELDS: dict[str, Sortable] = {
	"created_at": subroutine.db.models.work.Task.created_at,
	"updated_at": subroutine.db.models.work.Task.updated_at,
	# **What "most recently finished" means** (`#710`). `updated_at` is the tempting proxy and
	# it is wrong: editing a finished item reorders the page for a reason nobody did. The
	# column is maintained under §10.7 invariant 5 — non-null exactly when the status category
	# is finished — so descending order is finished-newest-first with everything open at the
	# end, NULLS LAST doing that for free.
	"completed_at": subroutine.db.models.work.Task.completed_at,
	"due_at": subroutine.db.models.work.Task.due_at,
	"planned_for": subroutine.db.models.work.Task.planned_for,
	"importance": subroutine.db.models.work.Task.importance,
	"urgency": subroutine.db.models.work.Task.urgency,
	"priority_score": Derived(
		expression=RANKING, read=carried, carried_on=subroutine.db.models.work.Task.rank
	),
	"ref": subroutine.db.models.work.Task.ref,
	"title": subroutine.db.models.work.Task.title,
}

#: Newest first, which is what "what have I got" means for a to-do list.
DEFAULT_TASK_ORDER = ("-created_at",)

#: What ``?order=`` accepts on a document listing. Shorter than a task's because most of that
#: vocabulary is about scheduling and §6.14 says a document is not scheduled — there is no
#: deadline to sort by and no priority to rank.
DOCUMENT_FIELDS: dict[str, Sortable] = {
	"created_at": subroutine.db.models.work.Document.created_at,
	"updated_at": subroutine.db.models.work.Document.updated_at,
	"title": subroutine.db.models.work.Document.title,
	"ref": subroutine.db.models.work.Document.ref,
}

#: The same default, for the same reason.
DEFAULT_DOCUMENT_ORDER = ("-created_at",)

#: What ``?order=`` accepts on a project listing. ``key`` is the one people think in.
#:
#: **Here rather than in ``api/projects.py``, where it lived until `#501`.** A vocabulary
#: declared inside the HTTP layer is reachable by one transport, so `GET /v1/projects` accepted
#: a sort that no client could ask for and none of the three lists above could be compared with
#: it. Projects were the odd one out rather than a special case, which is what made this a move.
PROJECT_FIELDS: dict[str, Sortable] = {
	"created_at": subroutine.db.models.project.Project.created_at,
	"updated_at": subroutine.db.models.project.Project.updated_at,
	"key": subroutine.db.models.project.Project.key,
	"title": subroutine.db.models.project.Project.title,
	"path": subroutine.db.models.project.Project.path,
}

#: **Not ``-created_at``**, unlike the two above, and the difference is the point: a project
#: listing is a *tree*. By path a child follows its parent and the shape can be printed without
#: the caller reassembling it (§8.4), where newest-first would interleave branches.
DEFAULT_PROJECT_ORDER = ("path",)

#: **These names are deliberately absent from :data:`VIEW_READERS`, and the two lists above are
#: deliberately checked against it.** A reader is only needed by a caller that merges pages it
#: has already been given and re-sorts them in Python, and nothing merges projects: all four of
#: the CLI's project listings ask one connection for one workspace and render a tree per place.
#: Adding readers now would be a control nothing reads, which is `#303`'s shape and where that
#: item's answer was to delete rather than to wire.
#:
#: What changes it is a *merged* project listing. Give ``key`` and ``path`` readers then, and
#: extend ``tests/test_ordering.py``'s subset check to this list at the same time — a sort field
#: a merged listing accepts and ignores is worse than one it refuses.

#: How to read each sortable field off a **rendered view**, for a caller sorting rows it has
#: already been given rather than a query it is about to run. ``subroutine list`` is that
#: caller: it merges a page per workspace per kind and has to re-sort the result, so the
#: ordering the user asked for has to survive a comparison made in Python.
#:
#: **``priority_score`` reads the view's ``rank``, never its ``priority_score``.** They are
#: deliberately different things — the view reports ``importance * urgency`` (§6.3), while an
#: *ordering* by that name is what §6.3a computes. Sorting a merged list by the published score
#: would put a part-ranked item back below an unranked one, which is the exact defect the bands
#: were added to fix, reintroduced one layer up and only in the merged case.
#:
#: **It reads rather than recomputes, which is `#569`'s change.** The rule used to be applied
#: again here, from ``importance`` and ``urgency`` on the view; an ordering that reads other
#: rows cannot be reapplied that way, and a client silently re-sorting a merged page back to a
#: rule the server no longer uses is a defect only the merged case would show.
#:
#: **A missing value is null here and raises in :func:`carried`, and that asymmetry is
#: deliberate.** The cursor reads a row this process just loaded, so an absent value can only
#: mean a query that forgot the loader option — a bug, and worth a loud one. A *view* arrives
#: over the wire from an instance that may be a release behind and not send the field at all,
#: and `#345` and `#482` are the rule that a newer client reads what an older instance said
#: rather than refusing it outright. Do not make this one strict to match the other.
#:
#: A name absent here is one no client can sort a merged page by; ``tests/test_ordering.py``
#: fails if :data:`TASK_FIELDS` ever grows one, because a sort field the CLI silently ignores
#: is worse than one it refuses.
VIEW_READERS: dict[str, typing.Callable[[typing.Any], typing.Any]] = {
	"created_at": lambda item: item.created_at,
	"updated_at": lambda item: item.updated_at,
	"completed_at": lambda item: getattr(item, "completed_at", None),
	"due_at": lambda item: getattr(item, "due_at", None),
	"planned_for": lambda item: getattr(item, "planned_for", None),
	"importance": lambda item: getattr(item, "importance", None),
	"urgency": lambda item: getattr(item, "urgency", None),
	"priority_score": lambda item: getattr(item, "rank", None),
	# **The one entry that recomputes rather than reads, and :func:`put_off` says why**: the
	# band is a fact about the clock at the moment the query ran, and a rendered row carries
	# the start date rather than the answer. A document has no start date and lands in the
	# first band, which is exactly what :data:`UNDEFERRABLE` gives it server-side.
	DEFERRED: put_off,
	"ref": lambda item: item.ref,
	"title": lambda item: item.title,
}

#: Stands in for a null while sorting, so that two rows tied at "no value" compare as equal
#: rather than raising. It is never ordered *against* a real value: the null flag is the first
#: element of every sort key and separates the two groups before this is reached.
_ABSENT = 0


def merged (
	rows: typing.Sequence[typing.Any],
	*,
	key: typing.Callable[[typing.Any], typing.Any],
	order: tuple[tuple[str, bool], ...],
) -> list[typing.Any]:
	"""Sort rows that arrived as several pages into the one order the caller asked for.

	**NULLS LAST in both directions**, matching what every query here does (SPEC.md §10.3) —
	so a document, which has no deadline and no priority, sorts last in a list ranked by
	either rather than first. That is the same answer the database gives and the same answer
	§6.3a gives an unranked task, which is why it needs no separate rule.

	Applied one field at a time from the last to the first, relying on Python's sort being
	stable. That is what lets each field carry its own direction: a single composite key
	cannot express "newest first, then title ascending" without inverting values by type.
	"""

	found = list(rows)

	for name, descending in reversed(order):
		read = VIEW_READERS[name]

		def sorted_by (
			row: typing.Any,
			read: typing.Callable[[typing.Any], typing.Any] = read,
			descending: bool = descending,
		) -> tuple[int, typing.Any]:
			"""Return one row's key for this field, with nulls at the end either way."""

			value = read(key(row))

			# Reversing puts the larger first, so a null has to be the *smaller* of the two
			# groups when descending in order to come out last. Both spellings say "nulls
			# last"; the flag is what makes the direction irrelevant to that promise.
			if value is None:
				return (0 if descending else 1, _ABSENT)

			return (1 if descending else 0, value)

		found.sort(key=sorted_by, reverse=descending)

	return found


def requested (
	expression: str | None,
	*,
	allowed: typing.Mapping[str, Sortable],
	default: typing.Sequence[str],
) -> tuple[tuple[str, bool], ...]:
	"""Turn ``-importance,due_at`` into ``(field, descending)`` pairs, refusing the unknown.

	Several fields, because "by priority, then by deadline" is the ordering people actually
	want and one column cannot express it. A leading ``-`` reverses that field alone.

	Only the *parsing*, so that the HTTP path can go on to build keyset cursors from the
	result while a client that pages by nothing at all can simply order a query. Both refuse
	the same names with the same message, which is the reason this is one function.
	"""

	names = [part.strip() for part in (expression or "").split(",") if part.strip()]
	chosen: list[tuple[str, bool]] = []

	for name in names or list(default):
		descending = name.startswith("-")
		bare = name[1:] if descending else name

		if bare not in allowed:
			raise subroutine.errors.ValidationError(
				# "this listing" rather than "this endpoint": the same refusal is read by
				# somebody who typed `subroutine list --order`, and they have no endpoint. A
				# message shared by two transports has to be true of both.
				f"{bare!r} is not a field this listing can sort by.",
				errors=[
					subroutine.errors.FieldError(
						field="order",
						code="invalid_field_value",
						message=f"Unknown sort field {bare!r}.",
						hint=f"Sortable fields are: {', '.join(sorted(allowed))}. Prefix one "
						f"with '-' to reverse it.",
					)
				],
			)

		if any(name == bare for name, _descending in chosen):
			raise subroutine.errors.ValidationError(
				f"{bare!r} appears twice in the ordering.",
				errors=[
					subroutine.errors.FieldError(
						field="order",
						code="invalid_field_value",
						message=f"Sort field {bare!r} is repeated.",
						hint="Each field may appear once; the order they appear in is the "
						"order they are applied.",
					)
				],
			)

		chosen.append((bare, descending))

	return tuple(chosen)


def column (
	field: Sortable,
) -> sqlalchemy.orm.InstrumentedAttribute[typing.Any] | sqlalchemy.ColumnElement[typing.Any]:
	"""Return the expression to sort by, whether the field is stored or computed."""

	return field.expression if isinstance(field, Derived) else field


def options (
	expression: str | None,
	*,
	allowed: typing.Mapping[str, Sortable],
	default: typing.Sequence[str],
) -> list[sqlalchemy.orm.interfaces.ORMOption]:
	"""Return the loader options an ordering needs, so its computed values reach each row.

	**Every query that sorts by one of these must apply them, and the pairing is the whole
	risk.** A field whose expression reads other rows has no Python half to fall back on
	(:func:`carried`), so a statement that orders by it and omits this loads rows with the
	value absent — and a keyset cursor built from those rows carries nothing, which paginates
	wrongly rather than failing. :func:`carried` raises instead of returning ``None``, so the
	mistake is loud; this is what stops it being made.

	Parsed from the same expression the ordering is, and refusing the same names, so the two
	cannot disagree about which fields were asked for.
	"""

	found: list[sqlalchemy.orm.interfaces.ORMOption] = []

	for name, _descending in requested(expression, allowed=allowed, default=default):
		chosen = allowed[name]

		if not isinstance(chosen, Derived):
			continue

		carrying = chosen.carrying()

		if carrying is not None:
			found.append(carrying)

	return found


def clauses (
	expression: str | None,
	*,
	allowed: typing.Mapping[str, Sortable],
	default: typing.Sequence[str],
	tiebreak: sqlalchemy.orm.InstrumentedAttribute[typing.Any],
) -> list[sqlalchemy.UnaryExpression[typing.Any]]:
	"""Return ``ORDER BY`` terms for a caller that is not paging with a cursor.

	``NULLS LAST`` in both directions, stated rather than left to the backend: SQLite and
	PostgreSQL disagree about the default, so an unqualified ``ORDER BY`` sorts differently
	depending on where it runs (SPEC.md §10.3).

	**The tiebreaker is appended always and is always ascending, which is oldest first**,
	because the primary key is a time-ordered UUID. It used to follow the last key's
	direction; Simon's decision of 2026-08-13 is that age is *"one of the least significant
	ordering fields, maybe the last"* and not a signal at all — *"we can't make a general
	decision about whether something is important because it's been in the backlog for more
	or less time"*. So it separates rows that tie and says nothing else, and a separator
	should not inherit a direction from a key it has nothing to do with.

	It matters more than it looks. Ranked listings here are tie-heavy — 52 of this project's
	172 open tasks share one score — so for a third of a backlog this is the only thing
	deciding the order, and under the old rule the most recently captured item won for ever.

	``api.pagination.parse_order`` states the same rule for a cursor, and the two must agree
	or a page boundary lands somewhere the next page does not start.
	"""

	keys = requested(expression, allowed=allowed, default=default)
	terms: list[sqlalchemy.UnaryExpression[typing.Any]] = []

	for name, descending in keys:
		found = column(allowed[name])
		terms.append(found.desc().nullslast() if descending else found.asc().nullslast())

	terms.append(tiebreak.asc().nullslast())

	return terms
