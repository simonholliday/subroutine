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

Cursors are signed with ``secret_key`` — the one thing SPEC.md §7.4 says that key is for.
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

import subroutine.errors

#: Separates the payload from its signature. Not base64url's alphabet, so it can never
#: appear inside either half.
_SEPARATOR = "."


@dataclasses.dataclass(frozen=True)
class Derived:
	"""A sort field computed from other columns rather than stored in one of its own.

	**Both halves are required, and that is the whole point of this class.** An ordering
	needs an expression the database can sort by; a *cursor* needs the same value read back
	off a loaded row, and a derived field has no attribute for :func:`encode` to look up.

	``priority_score`` was declared as a bare ``importance * urgency`` expression, which
	orders perfectly and has a ``.key`` of ``None``. So every ``?order=priority_score``
	request whose result set exceeded one page died in ``encode`` with ``TypeError:
	attribute name must be string, not 'NoneType'`` — the sort this API recommends to
	agents, failing precisely when there is enough work to page through. It survived
	because the only installation using it had fewer items than the default page size, and
	because the ``SORTABLE`` maps were annotated ``dict[str, typing.Any]``, which told the
	type checker to look away at exactly the declaration that was wrong.
	"""

	expression: sqlalchemy.ColumnElement[typing.Any]
	read: typing.Callable[[typing.Any], typing.Any]


#: What a ``SORTABLE`` map may hold: a column, or a computed field that knows how to read
#: itself back. Annotate those maps with this rather than with ``typing.Any``.
Sortable = sqlalchemy.orm.InstrumentedAttribute[typing.Any] | Derived


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

	requested = [part.strip() for part in (expression or "").split(",") if part.strip()]
	names = requested or list(default)
	keys: list[SortKey] = []

	for name in names:
		descending = name.startswith("-")
		bare = name[1:] if descending else name

		if bare not in allowed:
			raise subroutine.errors.ValidationError(
				f"{bare!r} is not a field this endpoint can sort by.",
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

		if any(key.name == bare for key in keys):
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

		chosen = allowed[bare]

		if isinstance(chosen, Derived):
			keys.append(
				SortKey(
					name=bare,
					column=chosen.expression,
					descending=descending,
					read=chosen.read,
				)
			)

		else:
			keys.append(SortKey(name=bare, column=chosen, descending=descending))

	# The tiebreaker follows the last key's direction, so that "newest first" stays newest
	# first among rows that tie.
	keys.append(SortKey(name="id", column=tiebreak, descending=keys[-1].descending if keys else True))

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
	secret: str, keys: typing.Sequence[SortKey], row: typing.Any
) -> str:
	"""Return a signed cursor naming one row's position in an ordering."""

	payload = {key.name: _to_json(key.value_of(row)) for key in keys}
	body = base64.urlsafe_b64encode(
		json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
	).decode("ascii")

	return f"{body}{_SEPARATOR}{_sign(secret, body)}"


def decode (
	secret: str, keys: typing.Sequence[SortKey], cursor: str
) -> list[typing.Any]:
	"""Return the sort values a cursor carries, refusing one that was not issued here.

	Every failure — a bad signature, unreadable base64, a cursor from a different ordering
	— reports the same way, because the caller's remedy is the same in all of them: start
	the listing again. Naming which check failed would tell somebody probing the signature
	how far they had got.
	"""

	body, separator, signature = cursor.partition(_SEPARATOR)

	if not separator or not hmac.compare_digest(signature, _sign(secret, body)):
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


def _sign (secret: str, body: str) -> str:
	"""Return the signature for a cursor body."""

	digest = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()

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
