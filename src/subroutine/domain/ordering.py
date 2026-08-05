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
import typing

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.db.models.work
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
	"""

	expression: sqlalchemy.ColumnElement[typing.Any]
	read: typing.Callable[[typing.Any], typing.Any]


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


def ranking (row: typing.Any) -> int | None:
	"""Return one task's place in §6.3a's three-band ordering, from a loaded row.

	The Python half of the pair. :data:`RANKING` is the same rule as SQL, and the two must
	agree exactly: this one names the row a cursor stopped at, that one orders the query, and
	a disagreement would be a page boundary that skips or repeats rows.
	"""

	# `row` is a loaded ORM object rather than a typed model, so the two axes arrive as
	# `Any`; read into locals so the arithmetic below is checked rather than waved through.
	importance: int | None = row.importance
	urgency: int | None = row.urgency

	if importance is not None and urgency is not None:
		return RANKED_BAND + importance * urgency

	if importance is not None:
		return PART_RANKED_BAND + importance

	if urgency is not None:
		return PART_RANKED_BAND + urgency

	return None


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
	"due_at": subroutine.db.models.work.Task.due_at,
	"planned_for": subroutine.db.models.work.Task.planned_for,
	"importance": subroutine.db.models.work.Task.importance,
	"urgency": subroutine.db.models.work.Task.urgency,
	"priority_score": Derived(expression=RANKING, read=ranking),
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
#: **``priority_score`` reads through :func:`ranking`, not off the view's field of that name.**
#: They are deliberately different things — the view reports ``importance * urgency`` (§6.3),
#: while an *ordering* by that name applies §6.3a's three bands. Sorting a merged list by the
#: view's field would put a part-ranked item back below an unranked one, which is the exact
#: defect the bands were added to fix, reintroduced one layer up and only in the merged case.
#:
#: A name absent here is one no client can sort a merged page by; ``tests/test_ordering.py``
#: fails if :data:`TASK_FIELDS` ever grows one, because a sort field the CLI silently ignores
#: is worse than one it refuses.
VIEW_READERS: dict[str, typing.Callable[[typing.Any], typing.Any]] = {
	"created_at": lambda item: item.created_at,
	"updated_at": lambda item: item.updated_at,
	"due_at": lambda item: getattr(item, "due_at", None),
	"planned_for": lambda item: getattr(item, "planned_for", None),
	"importance": lambda item: getattr(item, "importance", None),
	"urgency": lambda item: getattr(item, "urgency", None),
	"priority_score": lambda item: ranking(item) if hasattr(item, "importance") else None,
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

	The tiebreaker is appended always and follows the last key's direction, so that equal
	values keep one stable order and "newest first" stays newest first among rows that tie.
	"""

	keys = requested(expression, allowed=allowed, default=default)
	terms: list[sqlalchemy.UnaryExpression[typing.Any]] = []

	for name, descending in keys:
		found = column(allowed[name])
		terms.append(found.desc().nullslast() if descending else found.asc().nullslast())

	trailing = keys[-1][1] if keys else True
	terms.append(tiebreak.desc().nullslast() if trailing else tiebreak.asc().nullslast())

	return terms
