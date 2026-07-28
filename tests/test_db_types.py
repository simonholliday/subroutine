"""Portability tests for the column types.

These are the most valuable tests in the foundation slice. Each one covers a difference
between SQLite and PostgreSQL that is invisible on one of them, and each has a plausible
failure mode that would reach production without it.
"""

import datetime
import uuid
import zoneinfo

import pytest
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.orm

import sample_models
import subroutine.db.base
import subroutine.db.session
import subroutine.db.types


def test_aware_datetime_round_trips_identically (session: sqlalchemy.orm.Session) -> None:
	"""A timezone-aware instant survives storage and retrieval unchanged, on any backend.

	This is the single most valuable portability test in the project. A plain
	``DateTime(timezone=True)`` column drops ``tzinfo`` on SQLite, and every comparison
	made against the result afterwards is then wrong by the local UTC offset.
	"""

	london = zoneinfo.ZoneInfo("Europe/London")
	original = datetime.datetime(2026, 7, 28, 17, 30, 0, tzinfo=london)

	row = sample_models.SampleRow(moment=original)
	session.add(row)
	session.flush()
	session.expire(row)

	stored = session.get(sample_models.SampleRow, row.id)

	assert stored is not None
	assert stored.moment is not None
	assert stored.moment.tzinfo is not None
	assert stored.moment == original
	assert stored.moment.utcoffset() == datetime.timedelta(0)


def test_naive_datetime_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""Writing a naive datetime fails loudly rather than assuming it meant UTC."""

	session.add(sample_models.SampleRow(moment=datetime.datetime(2026, 7, 28, 17, 30, 0)))

	with pytest.raises(Exception, match="Naive datetime"):
		session.flush()


def test_calendar_date_round_trips (session: sqlalchemy.orm.Session) -> None:
	"""A calendar date is stored and returned as a date, with no time component."""

	row = sample_models.SampleRow(day=datetime.date(2026, 8, 2))
	session.add(row)
	session.flush()
	session.expire(row)

	stored = session.get(sample_models.SampleRow, row.id)

	assert stored is not None
	assert stored.day == datetime.date(2026, 8, 2)
	assert not isinstance(stored.day, datetime.datetime)


def test_datetime_is_refused_for_a_calendar_date (session: sqlalchemy.orm.Session) -> None:
	"""A datetime cannot sneak into a date column and be silently truncated."""

	session.add(sample_models.SampleRow(day=datetime.datetime(2026, 8, 2, 13, 0, tzinfo=datetime.UTC)))

	with pytest.raises(Exception, match="Datetime passed to a CalendarDate"):
		session.flush()


def test_uuid_round_trips_as_a_uuid (session: sqlalchemy.orm.Session) -> None:
	"""Identifiers come back as UUID objects, not strings, on either backend."""

	row = sample_models.SampleRow()
	session.add(row)
	session.flush()
	session.expire(row)

	stored = session.get(sample_models.SampleRow, row.id)

	assert stored is not None
	assert isinstance(stored.id, uuid.UUID)


def test_generated_uuids_sort_by_creation_order () -> None:
	"""Version 7 identifiers increase over time, so they cluster in the index."""

	generated = [subroutine.db.types.new_uuid() for _ in range(50)]

	assert [str(value) for value in generated] == sorted(str(value) for value in generated)
	assert len(set(generated)) == 50


def test_autoincrement_bigint_assigns_ascending_values (session: sqlalchemy.orm.Session) -> None:
	"""The sequence column auto-populates on both backends.

	SQLite only treats a primary key as an alias for its row id when the column is
	declared ``INTEGER``; a plain ``BIGINT`` silently stays NULL.
	"""

	first, second = sample_models.SampleEvent(), sample_models.SampleEvent()
	session.add_all([first, second])
	session.flush()

	assert first.seq is not None
	assert second.seq is not None
	assert second.seq > first.seq


def test_metadata_column_is_mapped_to_a_safe_attribute (session: sqlalchemy.orm.Session) -> None:
	"""The column is named ``metadata`` while the attribute is ``meta``.

	``metadata`` is reserved on a declarative class and raises at import time, so the
	mapping has to be explicit. This test fails at collection if anyone re-introduces it.
	"""

	row = sample_models.SampleRow(meta={"branch": "main", "pr": 42})
	session.add(row)
	session.flush()
	session.expire(row)

	stored = session.get(sample_models.SampleRow, row.id)

	assert stored is not None
	assert stored.meta == {"branch": "main", "pr": 42}
	assert "metadata" in sample_models.SampleRow.__table__.columns


def test_utcnow_is_aware_and_utc () -> None:
	"""The timestamp helper always produces an aware UTC value."""

	moment = subroutine.db.types.utcnow()

	assert moment.tzinfo is not None
	assert moment.utcoffset() == datetime.timedelta(0)


def test_constraint_naming_convention_is_applied () -> None:
	"""Constraints get deterministic names, which SQLite migrations depend on."""

	assert subroutine.db.base.Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"

	primary_key = sample_models.SampleRow.__table__.primary_key

	assert isinstance(primary_key, sqlalchemy.PrimaryKeyConstraint)
	assert primary_key.name == "pk_sample_row"


def test_foreign_keys_are_enforced_on_sqlite (
	engine: sqlalchemy.engine.Engine,
) -> None:
	"""SQLite has foreign key enforcement switched on for every pooled connection.

	It is off by default, which makes every foreign key in the schema decorative.
	"""

	if engine.dialect.name != "sqlite":
		pytest.skip("PostgreSQL always enforces foreign keys")

	with engine.connect() as connection:
		assert connection.execute(sqlalchemy.text("PRAGMA foreign_keys")).scalar() == 1
		assert str(connection.execute(sqlalchemy.text("PRAGMA journal_mode")).scalar()) == "wal"
