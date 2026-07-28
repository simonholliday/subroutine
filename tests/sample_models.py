"""Throwaway tables used to exercise the portable column types.

These live on their own metadata rather than the application's. Registering test-only
tables against ``Base.metadata`` would make them indistinguishable from real ones to
Alembic, and the migration-drift check would then report them as missing from every
migration — a false alarm that would train everyone to ignore a genuinely useful test.
"""

import datetime
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.types


class SampleBase(sqlalchemy.orm.DeclarativeBase):
	"""Declarative base for test-only tables, kept apart from the application schema."""

	metadata = sqlalchemy.MetaData(naming_convention=subroutine.db.base.NAMING_CONVENTION)


class SampleRow(SampleBase):
	"""A throwaway table exercising every portable column type."""

	__tablename__ = "sample_row"

	id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		primary_key=True,
		default=subroutine.db.types.new_uuid,
	)
	moment: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	day: sqlalchemy.orm.Mapped[datetime.date | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.CalendarDate(), nullable=True
	)
	label: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=True
	)
	meta: sqlalchemy.orm.Mapped[dict[str, object]] = sqlalchemy.orm.mapped_column(
		"metadata", subroutine.db.types.json_column(), default=dict
	)


class SampleEvent(SampleBase):
	"""Mirrors the real ``event`` table: a monotonic integer key plus a UUID.

	The sequence number is the primary key because that is the only way SQLite will
	auto-populate it — a non-key column declared ``autoincrement`` is silently ignored on
	both backends.
	"""

	__tablename__ = "sample_event"

	seq: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.autoincrement_bigint(), primary_key=True, autoincrement=True
	)
	id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), unique=True, default=subroutine.db.types.new_uuid
	)
