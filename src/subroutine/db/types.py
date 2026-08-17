"""Column types that behave identically on SQLite and PostgreSQL.

Every type here exists because the two backends disagree about something, and the
disagreement is the kind that passes tests on one and corrupts data on the other:

* SQLite has no timezone-aware datetime storage. A plain ``DateTime(timezone=True)``
  column silently discards ``tzinfo`` on the way in and hands back a naive value on the
  way out, which then compares wrongly against every aware datetime in the program.
* SQLite has no date storage class either, and no native UUID type.
* SQLite only treats a column as an auto-incrementing row id when it is declared
  ``INTEGER PRIMARY KEY`` — ``BIGINT`` does not qualify.
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.types
import uuid6


def new_uuid () -> uuid.UUID:
	"""Return a fresh time-ordered UUID for a new row.

	Version 7 rather than 4: the leading bits are a timestamp, so successive inserts land
	next to each other in the index instead of scattering across it. That keeps write
	throughput and cache locality sane as tables grow, and makes the value usable as a
	tie-breaker when sorting by creation order.
	"""

	return uuid6.uuid7()


class UtcDateTime(sqlalchemy.types.TypeDecorator[datetime.datetime]):
	"""A timestamp that is always timezone-aware and always UTC, on every backend.

	Naive datetimes are rejected on the way in rather than quietly assumed to be UTC —
	an assumption that is right often enough to hide the times it is wrong.
	"""

	impl = sqlalchemy.types.DateTime(timezone=True)
	cache_ok = True

	@property
	def python_type (self) -> type[datetime.datetime]:
		"""Report what a value of this column is, in Python.

		``TypeDecorator`` does not delegate this to ``impl``; left unimplemented it raises,
		and anything introspecting a column to decide how to read a value back gets an
		exception rather than an answer. Keyset pagination is the first caller to need it.
		"""

		return datetime.datetime

	def process_bind_param (
		self, value: datetime.datetime | None, dialect: sqlalchemy.Dialect
	) -> datetime.datetime | None:
		"""Normalise a value to UTC before it is written."""

		if value is None:
			return None

		if value.tzinfo is None:
			raise ValueError(
				"Naive datetime passed to a UtcDateTime column. Timestamps must carry a "
				"timezone; use subroutine.db.types.utcnow()."
			)

		return value.astimezone(datetime.UTC)

	def process_result_value (
		self, value: datetime.datetime | None, dialect: sqlalchemy.Dialect
	) -> datetime.datetime | None:
		"""Return a UTC-aware value, whatever the backend handed back."""

		if value is None:
			return None

		if value.tzinfo is None:
			# SQLite round-trips a naive value; the digits are UTC by construction.
			return value.replace(tzinfo=datetime.UTC)

		return value.astimezone(datetime.UTC)


class CalendarDate(sqlalchemy.types.TypeDecorator[datetime.date]):
	"""A calendar date with no time and no timezone.

	``datetime.datetime`` subclasses ``datetime.date``, so an accidental datetime would
	otherwise be accepted here and silently truncated — and a truncation is invisible, which
	is what makes it worth a type rather than a check.

	**No production column uses this today** (`#927`'s L-15). It was written for
	``task.planned_for``, which `#854` absorbed into ``starts_at`` — a column that carries a
	time, because an appointment has one — so the type outlived its only caller. Its docstring
	named ``task.starts_at`` for a while afterwards, which was the reverse of true: that column
	uses this type precisely nowhere, and could not.

	**Kept rather than deleted**, and `#858` is the reason: whether a defer is day-scale is an
	open question, and ``snoozed_until`` is where the answer would land. The migrations still
	reference it for the columns they created, so it cannot go while they can be replayed.
	"""

	impl = sqlalchemy.types.Date
	cache_ok = True

	@property
	def python_type (self) -> type[datetime.date]:
		"""Report what a value of this column is, in Python. See :class:`UtcDateTime`."""

		return datetime.date

	def process_bind_param (
		self, value: datetime.date | None, dialect: sqlalchemy.Dialect
	) -> datetime.date | None:
		"""Reject datetimes, allowing only true calendar dates through."""

		if value is None:
			return None

		if isinstance(value, datetime.datetime):
			raise ValueError(
				"Datetime passed to a CalendarDate column. Use a datetime.date; a "
				"planned day has no time of day and no timezone."
			)

		return value


def uuid_column () -> sqlalchemy.Uuid[uuid.UUID]:
	"""Return the UUID column type: native on PostgreSQL, CHAR(32) on SQLite."""

	return sqlalchemy.Uuid(as_uuid=True)


def autoincrement_bigint () -> sqlalchemy.types.TypeEngine[int]:
	"""Return a 64-bit auto-incrementing primary key type that works on both backends.

	PostgreSQL is happy with ``BIGINT``; SQLite only makes a column an alias for its
	internal row id when it is declared ``INTEGER``, so it gets that instead.
	"""

	return sqlalchemy.BigInteger().with_variant(sqlalchemy.Integer(), "sqlite")


def json_column () -> sqlalchemy.types.TypeEngine[typing.Any]:
	"""Return the JSON column type used for opaque, never-queried blobs.

	Deliberately not indexed or filtered on: JSON path support differs irreconcilably
	between the two backends, and a feature that performs differently depending on where
	it is deployed is worse than one that is honestly absent.
	"""

	return sqlalchemy.JSON()


def utcnow () -> datetime.datetime:
	"""Return the current time as a timezone-aware UTC datetime.

	Timestamps are generated in Python rather than by the database: the two backends
	differ in precision and in how they interpret their own clock functions, and a value
	that means something different depending on who wrote it is not much of a timestamp.
	"""

	return datetime.datetime.now(datetime.UTC)
