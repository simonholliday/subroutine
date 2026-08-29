"""Keyset pagination: stable pages over a moving table, and the cursors that address them.

An offset is the obvious way to paginate and the wrong one here. ``LIMIT 50 OFFSET 50``
re-runs the whole sort and then throws away the first fifty rows, and — worse for an agent
working through a backlog — anything created or completed between two requests shifts every
subsequent page, so items are silently skipped or seen twice. A keyset cursor says "carry on
after *this row*", which costs the same whether it is page two or page two hundred and does
not care what changed behind it.

The awkward part is NULLs, and it is genuinely awkward: a task with no due date has to sort
somewhere, and the two backends disagree about where by default — SQLite puts NULLs first,
PostgreSQL last. So every ordering here is explicitly ``NULLS LAST`` in both directions, and
the "after this row" predicate is written knowing that. A row with a NULL sort value is
therefore last in its group, ascending or descending, and paging through it works because
the final tiebreaker is the id, which is never null.

Cursors are signed with ``secret_key`` — the one thing docs/design.md §7.4 says that key is for.
They are opaque by intent: a client that parses one has coupled itself to the sort
implementation, and a client that *edits* one is choosing its own comparison values.
"""

import base64
import dataclasses
import datetime
import hashlib
import hmac
import json
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.domain.ordering
import subroutine.errors

#: Separates the payload from its signature. Not base64url's alphabet, so it can never
#: appear inside either half.
_SEPARATOR = "."


#: A computed sort field, and the union a sortable map may hold. Both defined in
#: ``domain.ordering`` because the *vocabulary* of an ordering is a fact about the domain
#: that every transport shares; what stays here is the part that belongs to HTTP — keyset
#: cursors, their signing, and the seek predicate.
Derived = subroutine.domain.ordering.Derived
Sortable = subroutine.domain.ordering.Sortable


@dataclasses.dataclass(frozen=True)
class SortKey:
	"""One column of an ordering, and which way it runs."""

	name: str
	column: sqlalchemy.orm.InstrumentedAttribute[typing.Any] | sqlalchemy.ColumnElement[typing.Any]
	descending: bool = False

	#: How to take this key's value off a loaded row, when it is not simply an attribute.
	#: ``None`` means the ordinary case: the column names an attribute of its own.
	read: typing.Callable[[typing.Any], typing.Any] | None = None

	def value_of (self, row: typing.Any) -> typing.Any:
		"""Return this key's value for one row, for a cursor to carry."""

		if self.read is not None:
			return self.read(row)

		attribute = self.column.key

		if attribute is None:
			# A computed expression that arrived without a reader. Unreachable through
			# `parse_order`, which builds one of these only from a `Derived` and `Derived`
			# demands the reader — so this is a wrong construction rather than bad input.
			# Stated here because the alternative was `getattr`'s "attribute name must be
			# string, not 'NoneType'", which named nothing a reader could act on.
			raise subroutine.errors.InternalError(
				f"The sort field {self.name!r} is computed and has no way to read itself "
				f"off a row.",
				hint="Declare it as a pagination.Derived, which requires both halves.",
			)

		return getattr(row, attribute)

	def ordering (self) -> sqlalchemy.UnaryExpression[typing.Any]:
		"""Return this key as an ``ORDER BY`` term.

		``NULLS LAST`` in both directions, and stated rather than left to the backend: the
		default differs between SQLite and PostgreSQL, so an unqualified ``ORDER BY`` here
		is a query that paginates differently depending on where it runs.
		"""

		direction = self.column.desc() if self.descending else self.column.asc()

		return direction.nullslast()


def parse_order (
	expression: str | None,
	*,
	allowed: typing.Mapping[str, Sortable],
	default: typing.Sequence[str],
	tiebreak: sqlalchemy.orm.InstrumentedAttribute[typing.Any],
) -> tuple[SortKey, ...]:
	"""Turn ``-importance,due_at`` into an ordering, refusing anything unrecognised.

	Several fields, because "by priority, then by deadline" is the ordering people actually
	want and one column cannot express it. A leading ``-`` reverses that field alone.

	``tiebreak`` is appended always, and is what makes the ordering *total*: without a
	unique last key, two rows with equal sort values have no defined order between them,
	and a cursor pointing at one of them cannot say which side of it the next page starts.
	"""

	# Parsed by the domain, which is where the vocabulary and the refusals live, so an
	# ordering means the same thing and is refused the same way whichever transport asked.
	keys: list[SortKey] = []

	for bare, descending in subroutine.domain.ordering.requested(
		expression, allowed=allowed, default=default
	):
		chosen = allowed[bare]

		if isinstance(chosen, Derived):
			keys.append(
				SortKey(
					name=bare, column=chosen.expression, descending=descending, read=chosen.read
				)
			)

		else:
			keys.append(SortKey(name=bare, column=chosen, descending=descending))

	# **Always ascending, which is oldest first**, because the primary key is a time-ordered
	# UUID. It used to follow the last key's direction; Simon's decision of 2026-08-13 is that
	# age is *"one of the least significant ordering fields, maybe the last"* and not a signal
	# at all — *"we can't make a general decision about whether something is important because
	# it's been in the backlog for more or less time"*. A separator should not inherit a
	# direction from a key it has nothing to do with, and a list where the newest thing always
	# wins a tie never finishes anything old. `ordering.clauses` says the same for the query
	# a client builds without a cursor, and the two must agree or a page boundary moves.
	keys.append(SortKey(name="id", column=tiebreak, descending=False))

	return tuple(keys)


def after (keys: typing.Sequence[SortKey], values: typing.Sequence[typing.Any]) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate selecting rows strictly after the one a cursor names.

	The lexicographic form: equal on every earlier key, and past it on this one, for each
	position in turn. Written out rather than using a row-value comparison, because a row
	constructor cannot express mixed directions and does not do what anyone expects with
	NULLs.

	With ``NULLS LAST`` a null sorts after every value, in both directions. So "past this
	one" is ``column > value`` (or ``<`` descending) **or** the column being null — and a
	cursor whose own value is null has nothing after it on that key at all, which is why
	that case contributes no term.
	"""

	clauses: list[sqlalchemy.ColumnElement[bool]] = []

	for index, key in enumerate(keys):
		earlier = [_same(keys[position], values[position]) for position in range(index)]
		value = values[index]

		if value is None:
			# Nulls are last and indistinguishable from each other; only a later key can
			# separate two of them.
			continue

		past = key.column < value if key.descending else key.column > value
		clauses.append(sqlalchemy.and_(*earlier, sqlalchemy.or_(past, key.column.is_(None))))

	if not clauses:
		# Every sort value was null, so the only rows after this one are those the
		# tiebreaker separates — and the tiebreaker is never null, so this cannot happen.
		return sqlalchemy.false()

	return sqlalchemy.or_(*clauses)


def encode (
	secret: str, keys: typing.Sequence[SortKey], row: typing.Any, *, collection: str
) -> str:
	"""Return a signed cursor naming one row's position in one collection's ordering.

	``collection`` is what the cursor is *for*, and it is bound into the signature rather than
	written into the payload — see :func:`decode` for why that is the whole of the check.
	"""

	payload = {key.name: _to_json(key.value_of(row)) for key in keys}
	body = base64.urlsafe_b64encode(
		json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
	).decode("ascii")

	return f"{body}{_SEPARATOR}{_sign(secret, body, collection)}"


def decode (
	secret: str, keys: typing.Sequence[SortKey], cursor: str, *, collection: str
) -> list[typing.Any]:
	"""Return the sort values a cursor carries, refusing one that was not issued here.

	Every failure — a bad signature, unreadable base64, a cursor from a different ordering
	or from a different collection — reports the same way, because the caller's remedy is the
	same in all of them: start the listing again. Naming which check failed would tell somebody
	probing the signature how far they had got.

	**A cursor is bound to the collection it was minted for** (`SR#1564`). The shape check below
	compares the *ordering* and could not tell two listings apart, and ``/v1/tasks`` and
	``/v1/documents`` share a default ordering — ``created_at`` plus the ``id`` tiebreak — so a
	cursor minted on one was accepted by the other and **silently omitted rows behind a 200**.
	Measured: thirteen documents, eight returned, five gone. An agent paging several listings in
	one loop, which ``/v1/docs/examples`` encourages, would read a short collection as a
	complete one.

	**Bound into the signature rather than added to the payload**, which is what makes it a
	check nobody can forget to perform: there is no second comparison to omit, a cursor cannot
	be edited to claim a different collection, and the refusal is the one every other failure
	already produces rather than a new branch reporting a new thing.
	"""

	body, separator, signature = cursor.partition(_SEPARATOR)

	if not separator or not hmac.compare_digest(signature, _sign(secret, body, collection)):
		raise _unusable()

	try:
		payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))

	except (ValueError, TypeError) as error:
		raise _unusable() from error

	if not isinstance(payload, dict) or set(payload) != {key.name for key in keys}:
		# The ordering changed between requests, so the cursor addresses a position in a
		# sequence that no longer exists.
		raise _unusable()

	return [_from_json(key, payload[key.name]) for key in keys]


def _same (key: SortKey, value: typing.Any) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate for a column equalling a cursor value, nulls included.

	``column == None`` renders as ``= NULL``, which is never true. The distinction matters
	here more than usual: it is what lets a page continue through a run of rows that all
	have no due date.
	"""

	return key.column.is_(None) if value is None else key.column == value


def _sign (secret: str, body: str, collection: str) -> str:
	"""Return the signature for a cursor body, in the collection it belongs to.

	The separator cannot appear in a collection name — they are the fixed words below — so
	there is no pair of (collection, body) that signs the same as another.
	"""

	signed = f"{collection}{_SEPARATOR}{body}"
	digest = hmac.new(secret.encode("utf-8"), signed.encode("ascii"), hashlib.sha256).digest()

	return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _unusable () -> subroutine.errors.ValidationError:
	"""Return the one refusal every bad cursor gets."""

	return subroutine.errors.ValidationError(
		"That cursor cannot be used.",
		errors=[
			subroutine.errors.FieldError(
				field="cursor",
				code="invalid_field_value",
				message="The cursor was not issued by this instance, or the ordering it "
				"belongs to has changed.",
				hint="Request the first page again, without a cursor, and follow "
				"'page.next_cursor' from there.",
			)
		],
	)


def _to_json (value: typing.Any) -> typing.Any:
	"""Render a sort value in a form JSON can carry."""

	if isinstance(value, datetime.datetime | datetime.date):
		return value.isoformat()

	if isinstance(value, uuid.UUID):
		return str(value)

	return value


def _from_json (key: SortKey, value: typing.Any) -> typing.Any:
	"""Read a sort value back, in the type its column compares against.

	Driven by the column's own type rather than by guessing from the JSON: a date and a
	datetime are both strings once encoded, and comparing the wrong one against a timestamp
	column fails differently on each backend.
	"""

	if value is None:
		return None

	try:
		python_type = key.column.type.python_type

	except NotImplementedError:
		# A column type that cannot say what it holds. The value goes back as it arrived,
		# which is right for anything JSON already represents faithfully — and a cursor is
		# not the place to raise about a type nobody has taught this to read.
		return value

	try:
		if python_type is datetime.datetime:
			return datetime.datetime.fromisoformat(str(value))

		if python_type is datetime.date:
			return datetime.date.fromisoformat(str(value))

		if python_type is uuid.UUID:
			return uuid.UUID(str(value))

		if python_type is int:
			return int(value)

	except (ValueError, TypeError) as error:
		raise _unusable() from error

	return value
