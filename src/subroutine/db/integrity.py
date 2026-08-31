"""Whether the rows in a database still point at rows that are there.

This exists because of one thing ``migrations/env.py`` has to do: SQLite cannot alter most
things in place, so Alembic rebuilds a table by copy-drop-rename, and that drop is a
foreign-key violation the moment another table holds a row pointing at it. Enforcement is
therefore off for the duration of a migration, which is what Alembic's batch mode calls for.

**The consequence is that a migration deleting a row somebody still refers to succeeds on
SQLite and is refused on PostgreSQL** — the one backend difference where the damage outlives
the command, and the default backend is the silent one. So the check the database would have
made is made here instead, at the end, before anything is committed.
"""

import collections
import typing

import sqlalchemy

#: A row that points at a row which is not there, named by the two tables. Deliberately not
#: by ``rowid``: a migration rebuilds tables, so the same broken row is a different rowid
#: either side of one and a comparison keyed on it would report every rebuild as damage.
Dangling = tuple[str, str]


def dangling_references (connection: sqlalchemy.Connection) -> tuple[Dangling, ...]:
	"""Return one entry per row that points at a row which no longer exists.

	Answers for SQLite and returns nothing for every other backend, which is not a gap.
	PostgreSQL enforces its foreign keys while the migration runs and refuses at the
	statement that does the damage, naming it — so there is nothing left for this to find,
	and a scan of every table would be paid for on every upgrade to say so.
	"""

	if connection.dialect.name != "sqlite":
		return ()

	# ``PRAGMA foreign_key_check`` reports (table, rowid, table it points at, which key).
	# It works inside a transaction and with enforcement switched off, which is what makes
	# it usable here at all — measured, because ``PRAGMA foreign_keys=ON`` in the same
	# position is accepted and silently ignored.
	found: typing.Any = connection.exec_driver_sql("PRAGMA foreign_key_check").all()

	return tuple((str(row[0]), str(row[2])) for row in found)


def appeared (before: typing.Sequence[Dangling], after: typing.Sequence[Dangling]) -> tuple[Dangling, ...]:
	"""Return what is broken in ``after`` and was not broken in ``before``.

	A difference rather than a count of what is broken now, so that a database some earlier
	migration already damaged can still be migrated. Refusing on the total would leave such
	an installation unable to move in either direction — including up, towards the version
	that stops it happening again.
	"""

	was = collections.Counter(before)
	now = collections.Counter(after)

	return tuple(sorted((now - was).elements()))


def in_words (found: typing.Sequence[Dangling]) -> str:
	"""Say what these broken references are, in a sentence a person can act on."""

	counted = collections.Counter(found)

	return ", ".join(
		f"{count} row{'' if count == 1 else 's'} in '{table}' "
		f"point{'s' if count == 1 else ''} at a row in '{parent}' that is not there"
		for (table, parent), count in sorted(counted.items())
	)
