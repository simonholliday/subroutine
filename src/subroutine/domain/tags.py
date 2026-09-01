"""Labels, created the first time somebody uses one.

``#health`` in a captured line should not require a separate tag-management step, from a
person or from an agent (docs/design.md §5.8). So there is no "create tag" command in the personal
path at all: applying a tag that does not exist creates it, and that is the only way tags
come into being until a UI offers to rename one.

Matching is on a normalised form — lower-cased, whitespace collapsed — so ``#Health`` and
``#health`` are one tag, while the name is stored as first written so a UI can show it the
way its author meant it.
"""

import re
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.text
import subroutine.errors

#: docs/design.md §10.6's column width. Enforced here so an over-long tag names itself rather than
#: arriving as a driver error on PostgreSQL and as silent success on SQLite (§10.3).
MAX_NAME_LENGTH = 128

_WHITESPACE = re.compile(r"\s+")

#: What a tag's name may not contain — `#1804`, and it is declared *here* rather than where it
#: is used.
#:
#: ``tag.in=ops,web`` narrows a listing to either of two tags, so a comma inside a name would
#: make *one tag called "ops,web"* and *two tags* the same string. :data:`subroutine.domain.
#: filtering.IN_SEPARATOR` is this value; that module imports this one, because compiling a
#: ``tag`` filter needs :func:`carrying` and the dependency can only run one way.
#:
#: **Simon took the consequence on 2026-09-01 with the cost measured first**: the `projects`
#: workspace holds 34 tags and not one contains a comma or a space. Project keys, status keys,
#: type keys and usernames are already constrained and cannot hold one either.
REFUSED_IN_A_NAME = ","


def normalize (name: str) -> str:
	"""Return the form two tags are considered the same by."""

	return _WHITESPACE.sub(" ", name).strip().lower()


def refuse_a_reference (name: str) -> None:
	"""Refuse a tag whose name is entirely digits.

	``#`` means both things: a tag in quick capture (§6.13) and a reference to an item in
	prose (§6.15). They stay apart because a reference is *all* digits and a tag is not —
	so a tag named "42" could never be written with its own sigil, and ``#42`` in anybody's
	description would go on pointing at task 42 instead.

	Enforced here rather than only in the two parsers because a rule that lives in a regex is
	a rule the next entry point does not have.

	**Public, and it was not** (`#1167`). This was private on the belief that :func:`ensure`
	is the one function every tag passes through — which is true of every tag that is
	*created* and false of one that is *renamed*. ``vocabulary.update_tag`` is the second
	door; it went round this for as long as it existed, and its own comment said it did not.

	**And a comma is refused for the same kind of reason, since `#1804`.** ``tag.in=ops,web``
	asks for either of two tags, so a comma inside a name would make *one tag called "ops,web"*
	and *two tags* the same string — an ambiguity a filter has no way to resolve and a caller
	no way to escape. Simon took that consequence on 2026-09-01 with the cost measured first:
	the `projects` workspace holds 34 tags and not one contains a comma.

	**Refused here rather than only in the parser**, which is this function's whole argument —
	a rule that lives in a regex is a rule the next entry point does not have, and renaming was
	the door the digit rule was missed at.
	"""

	if REFUSED_IN_A_NAME in name:
		raise subroutine.errors.ValidationError(
			f"{name!r} cannot be used as a tag.",
			errors=[
				subroutine.errors.FieldError(
					field="tags",
					code="invalid_field_value",
					message=(
						f"A tag holding a {REFUSED_IN_A_NAME!r} could not be told from two "
						f"tags when a listing is narrowed to either."
					),
					hint=(
						"Use a hyphen — 'ops-web' rather than 'ops,web' — or make them two "
						"tags and ask for both with 'tag.in=ops,web'."
					),
				)
			],
		)

	if not name.isdigit():
		return

	raise subroutine.errors.ValidationError(
		f"{name!r} cannot be used as a tag.",
		errors=[
			subroutine.errors.FieldError(
				field="tags",
				code="invalid_field_value",
				message=f"A tag made only of digits would be indistinguishable from a "
				f"reference to item #{name}.",
				hint="Add a letter — 'q3' rather than '3' — or, if you meant to refer to "
				f"item #{name}, write that in the description instead.",
			)
		],
	)


def ensure (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	names: typing.Sequence[str],
) -> list[subroutine.db.models.vocabulary.Tag]:
	"""Return the tags with these names, creating any that do not exist yet.

	Order follows ``names``, and duplicates collapse — ``#health #Health`` is one tag, not
	an error, because refusing it would be pedantry about a convenience feature.
	"""

	model = subroutine.db.models.vocabulary.Tag

	wanted: dict[str, str] = {}

	for name in names:
		cleaned = subroutine.domain.text.fit(
			subroutine.domain.text.require(name, field="tags", label="tag"),
			field="tags",
			limit=MAX_NAME_LENGTH,
			label="tag",
		)
		key = normalize(cleaned)

		refuse_a_reference(key)
		wanted.setdefault(key, cleaned)

	if not wanted:
		return []

	existing = {
		tag.name_normalized: tag
		for tag in session.scalars(
			sqlalchemy.select(model).where(
				model.workspace_id == workspace_id, model.name_normalized.in_(wanted)
			)
		)
	}

	resolved = []

	for key, written in wanted.items():
		tag = existing.get(key)

		if tag is None:
			tag = model(
				id=subroutine.db.types.new_uuid(),
				workspace_id=workspace_id,
				name=written,
				name_normalized=key,
			)
			session.add(tag)

		resolved.append(tag)

	session.flush()

	return resolved


class Joined (typing.NamedTuple):
	"""How one kind of item is joined to the tags on it."""

	#: The association model — ``TaskTag``, ``DocumentTag``.
	rows: typing.Any

	#: Its column naming the item, so a query can be written once for both.
	owner: typing.Any


#: Which kinds carry tags, keyed by the item's own model — `#819`.
#:
#: **One tag vocabulary across both, decided with Simon on 2026-08-12**, and it is what the
#: schema already assumed: both association tables reference ``tag.id``, and a tag is scoped to
#: a *workspace* rather than to a kind. So ``#health`` on a document and ``#health`` on a task
#: are the same tag, unlike a status or an item type, which §5.5 keeps per kind.
#:
#: **Keyed by the item rather than passed as a pair**, so a caller hands over the thing it has
#: and cannot put a document's id into a task's join table. That mattered enough to shape the
#: signatures: every function below takes the item.
#:
#: ``document_tag`` has existed since the initial migration and was read and written by nothing
#: until this — the second signature defect of this codebase (`#247`, `#251`, `#303`, `#443`),
#: and the largest instance of it so far, because the table was not merely unused: the guard
#: written to notice exactly that carried an excuse for it naming a field that did not exist
#: (`#820`).
JOINS: dict[typing.Any, Joined] = {
	subroutine.db.models.work.Task: Joined(
		rows=subroutine.db.models.work.TaskTag,
		owner=subroutine.db.models.work.TaskTag.task_id,
	),
	subroutine.db.models.work.Document: Joined(
		rows=subroutine.db.models.work.DocumentTag,
		owner=subroutine.db.models.work.DocumentTag.document_id,
	),
}


def _joined (item: typing.Any) -> Joined:
	"""Return how this item is joined to its tags, refusing a kind that carries none."""

	found = JOINS.get(type(item))

	if found is None:
		raise TypeError(f"{type(item).__name__} does not carry tags")

	return found


def apply_to (
	session: sqlalchemy.orm.Session,
	item: typing.Any,
	tags: typing.Sequence[subroutine.db.models.vocabulary.Tag],
) -> None:
	"""Attach tags to an item, skipping any it already carries.

	Idempotent rather than clever: the join row's primary key would refuse a duplicate, and
	a caller re-applying a tag has not done anything wrong.
	"""

	if not tags:
		return

	join = _joined(item)

	already = set(
		session.scalars(
			sqlalchemy.select(join.rows.tag_id).where(join.owner == item.id)
		)
	)

	for tag in tags:
		if tag.id in already:
			continue

		session.add(join.rows(**{join.owner.key: item.id, "tag_id": tag.id}))
		already.add(tag.id)

	session.flush()


def on (
	session: sqlalchemy.orm.Session, item: typing.Any
) -> list[subroutine.db.models.vocabulary.Tag]:
	"""Return an item's tags, in a stable order."""

	tag = subroutine.db.models.vocabulary.Tag
	join = _joined(item)

	return list(
		session.scalars(
			sqlalchemy.select(tag)
			.join(join.rows, join.rows.tag_id == tag.id)
			.where(join.owner == item.id)
			.order_by(tag.name_normalized)
		)
	)


def carrying (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	name: str,
	*,
	joined: typing.Any,
	holder: typing.Any,
) -> typing.Any:
	"""Return the predicate that narrows a listing to one tag, refusing a name nobody uses.

	**A tag was write-only until now** (`#1319`, Simon's decision of 2026-08-28). It is
	captured from a `#word`, stored, rendered on a row and on `show`, and published in the
	API's view — and no surface could select by one. `domain/filtering.py` contained the word
	*tag* zero times, search reads the title and the description alone, and a join row is
	neither. So somebody following the README wrote tags for months and could not get them
	back out.

	**A narrowing rather than a search**, which is the half `#1020` frames and that decision
	settles: search answers *find me something about this*, a tag answers *show me this set*.

	**Refused by name rather than answered empty.** A tag nobody has used and a tag spelled
	wrongly produce the same empty listing, and the second is far commoner — this is the same
	argument `status_for` makes about a status key, and `#1468`'s about a vocabulary word that
	is nowhere.

	``joined`` is the join model — :class:`~subroutine.db.models.work.TaskTag` or
	:class:`~subroutine.db.models.work.DocumentTag` — and ``holder`` is its column naming the
	item, which the two spell differently. Both are passed rather than derived: reading the
	item column off the primary key would work on the two tables that exist and fail obscurely
	on the third, and a listing writing its own copy of this predicate is how the two would
	come to disagree about what a tag match means.

	**Matched on the normalised name**, so `#Home` finds what `#home` tagged: that is what
	:func:`ensure` stores and what makes a tag one thing rather than several spellings of one.
	"""

	model = subroutine.db.models.vocabulary.Tag
	wanted = normalize(name)
	found = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace_id, model.name_normalized == wanted
		)
	).one_or_none()

	if found is None:
		raise subroutine.errors.ValidationError(
			f"No tag called {name!r} is used here.",
			code="invalid_field_value",
			errors=[
				subroutine.errors.FieldError(
					field="tag",
					code="invalid_field_value",
					message=f"Nothing is tagged {name!r} in this workspace.",
					hint="A tag exists once something carries it — add one by writing "
					"'#name' in a captured line.",
				)
			],
		)

	return sqlalchemy.select(holder).where(joined.tag_id == found.id)


def names_for (
	session: sqlalchemy.orm.Session,
	kind: typing.Any,
	identifiers: typing.Iterable[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
	"""Return the tag names on each of these items, as one query keyed by id.

	The batched form of :func:`on`, for rendering a page. Calling that one per row is fifty
	queries for a listing of fifty, and a listing is the thing this program does most.

	**The only one that takes the kind rather than the item**, because it is handed ids — a
	renderer has already loaded its rows and knows what they are, and asking it for one item
	back just to read its class would be a query to save an argument.

	Only the names are read. A renderer needs the word, never the row, and loading whole tag
	objects to take one string off each is a cost paid on every page.
	"""

	wanted = {identifier for identifier in identifiers if identifier is not None}

	if not wanted:
		return {}

	tag = subroutine.db.models.vocabulary.Tag
	join = JOINS[kind]

	rows = session.execute(
		sqlalchemy.select(join.owner, tag.name, tag.name_normalized)
		.join(tag, tag.id == join.rows.tag_id)
		.where(join.owner.in_(wanted))
	).all()

	found: dict[uuid.UUID, list[tuple[str, str]]] = {}

	for owner_id, name, normalized in rows:
		found.setdefault(owner_id, []).append((normalized, name))

	# **Sorted here, not by the database.** PostgreSQL's collation on this machine is
	# `en_GB.UTF-8` and does not sort byte-wise, so `ORDER BY name_normalized` put `ähnlich`
	# before `apple` there and after `zebra` on SQLite — measured. `tags` is a published API
	# field and a compact-line column, so the same task rendered `#ähnlich #apple` on one
	# deployment and `#apple #ähnlich` on the other, and the transport-equivalence test could
	# not see it because both of its sides run on one backend (docs/design.md §10.3).
	return {
		owner_id: [name for _key, name in sorted(pairs)] for owner_id, pairs in found.items()
	}


def set_on (
	session: sqlalchemy.orm.Session,
	item: typing.Any,
	tags: typing.Sequence[subroutine.db.models.vocabulary.Tag],
) -> None:
	"""Make an item's tags exactly these, adding what is missing and removing what is not.

	**Replaces rather than adds**, which is what §8.3 means by a field on a ``PATCH``: every
	other field there is assigned, not merged, and a ``tags`` that merged would be the only
	one a caller could not use to *remove* anything. An empty sequence therefore clears them,
	which is the same statement as sending ``null`` for a scalar.

	The counterpart is :func:`apply_to`, which is additive and is what quick capture wants —
	``#health`` in a captured line adds a tag to whatever is already there.

	Rows are added and deleted rather than the set being rebuilt, so a tag an item already
	carries keeps its join row. Nothing depends on that yet; it will the moment anything
	records when a tag was applied.
	"""

	join = _joined(item)
	wanted = {tag.id for tag in tags}

	already = set(
		session.scalars(sqlalchemy.select(join.rows.tag_id).where(join.owner == item.id))
	)

	for tag_id in already - wanted:
		session.execute(
			sqlalchemy.delete(join.rows).where(join.owner == item.id, join.rows.tag_id == tag_id)
		)

	for tag in tags:
		if tag.id not in already:
			session.add(join.rows(**{join.owner.key: item.id, "tag_id": tag.id}))

	session.flush()


def names_on (
	session: sqlalchemy.orm.Session, item: typing.Any
) -> list[str]:
	"""Return an item's tag names, ordered, for comparing one state against another.

	Its own function because an event's before-and-after has to be built the same way twice,
	and a sorted list of names is what makes "did the tags change" a value comparison rather
	than a set of join rows nobody can diff.
	"""

	return [tag.name for tag in on(session, item)]


#: Which join table and which of its columns names the item, for each kind that can be tagged.
#:
#: **Declared rather than derived**, for the reason :func:`carrying` states: reading the item
#: column off the primary key works on the two tables that exist and would fail obscurely on a
#: third. **Declared here rather than passed**, unlike :func:`carrying`, because the caller is
#: :mod:`subroutine.domain.search`, which knows an item's ``entity_type`` and nothing about its
#: tables — handing it two more parameters at four call sites would spread that knowledge
#: rather than keep it in the module that owns tags.
JOINED_BY: dict[str, tuple[typing.Any, typing.Any]] = {
	"task": (
		subroutine.db.models.work.TaskTag,
		subroutine.db.models.work.TaskTag.task_id,
	),
	"document": (
		subroutine.db.models.work.DocumentTag,
		subroutine.db.models.work.DocumentTag.document_id,
	),
}


def carried_by_name (
	entity_type: str, names: typing.Sequence[str]
) -> sqlalchemy.Select[tuple[typing.Any]] | None:
	"""Return the ids of items carrying every one of these tags, or ``None`` for no question.

	**Matched on the name rather than resolved to a row first, which is the whole difference
	from :func:`carrying`** (`#1576`). That one answers *show me this set* and refuses a name
	nothing uses, because a typo and an unused tag produce the same empty listing. This one is
	one branch of a search, which answers *find me something about this* — so a name nothing
	uses has to contribute nothing and let the text half answer, rather than turn the whole
	query down.

	**Every named tag must be carried**, which is :func:`subroutine.domain.search.matching`'s
	rule for words said about tags: a second one narrows rather than widens. Counted with
	``distinct`` because two workspaces may each hold a tag of the same name, and an item
	carrying one of them has satisfied that name once.

	``None`` rather than an empty select where there is nothing to ask, so the caller adds no
	branch at all — a select of no rows would be a union arm that can never match, which costs
	a scan to prove.
	"""

	wanted = sorted({normalize(one) for one in names} - {""})

	if not wanted:
		return None

	joined, holder = JOINED_BY[entity_type]
	model = subroutine.db.models.vocabulary.Tag

	return (
		sqlalchemy.select(holder)
		.join(model, model.id == joined.tag_id)
		.where(model.name_normalized.in_(wanted))
		.group_by(holder)
		.having(sqlalchemy.func.count(sqlalchemy.distinct(joined.tag_id)) == len(wanted))
	)
