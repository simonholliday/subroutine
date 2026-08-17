"""Moving an instance's data from one database to another — docs/design.md §12.6c, item ``#155``.

**Why this exists at all.** ``docs/hosting.md`` told somebody when to move to PostgreSQL and
how to point ``database_url`` at it, and never how the data got there — so following the
document exactly produced an empty PostgreSQL and a SQLite file nothing was reading. That is
worse than an undocumented feature, because the document leads people to it. §12.6's backups
are per-*engine* (``VACUUM INTO`` for SQLite, ``pg_dump`` for PostgreSQL), so the move a
reader would guess is the one that cannot work.

**A copy, not a move**, and the name says so. Nothing here writes to the source or deletes
anything: the operator points their configuration at the new database when they are satisfied,
and until then they have two. Changing engines is the kind of thing people do once, nervously,
on an evening — the reassurance that the original is untouched is most of the value.

**Not the export in `#157`, and the two are not competing.** This carries *the schema this
build understands*, table for table, so it is lossless by construction and needs no format to
design, version or defend. An export is a portable document somebody reads in ten years with
something that is not Subroutine. Different guarantees, different audiences; the recommendation
in `#155` originally said the export should serve both, and this is a reversal of it — a public
format is a contract, and one invented to solve an engine change would be a bad one.

**Both directions, deliberately.** PostgreSQL to SQLite is the same code and is what somebody
moving *back*, or making a laptop copy of a served instance, needs. It also means the dual
backend suite exercises this in both directions rather than in the one we happened to write.
"""

import dataclasses
import typing

import sqlalchemy
import sqlalchemy.engine

import subroutine.db.base
import subroutine.db.migrate
import subroutine.db.session
import subroutine.errors

#: How many rows are read and written at a time. The `event` table is the one that grows
#: without bound, and a straight `select().all()` on a busy instance is the whole table in
#: memory twice. Large enough that the round trips do not dominate, small enough to stay
#: uninteresting on a small machine.
BATCH = 500


@dataclasses.dataclass(frozen=True)
class Copied:
	"""What a transfer moved, per table, so the report is a measurement."""

	counts: dict[str, int]

	@property
	def rows (self) -> int:
		"""Total rows written."""

		return sum(self.counts.values())


def copy_into (source_url: str, target_url: str) -> Copied:
	"""Copy every row from one database into another, and check that it arrived.

	Refuses rather than guesses in three places, because each of them is a way to lose data
	quietly: a source that is not at this build's schema head, a target that already holds
	rows, and a count that does not match after the write.
	"""

	if _same_database(source_url, target_url):
		raise subroutine.errors.ValidationError(
			"The source and the target are the same database.",
			hint="Name a different database with --to.",
		)

	source = subroutine.db.session.create_engine(source_url)

	try:
		if not subroutine.db.migrate.is_up_to_date(source):
			raise subroutine.errors.ValidationError(
				"This database is not at the schema this build expects.",
				hint="Run 'subroutine db upgrade' first, then copy.",
			)

		target = subroutine.db.session.create_engine(target_url)

		try:
			_prepare(target_url, target)

			counts = _move(source, target)

			_verify(source, target, counts)

			return Copied(counts=counts)

		finally:
			target.dispose()

	finally:
		source.dispose()


def _same_database (source_url: str, target_url: str) -> bool:
	"""Whether two URLs name the same place, ignoring how the password is rendered."""

	source = sqlalchemy.engine.make_url(source_url)
	target = sqlalchemy.engine.make_url(target_url)

	return (
		source.get_backend_name() == target.get_backend_name()
		and source.host == target.host
		and source.port == target.port
		and source.database == target.database
	)


def _prepare (target_url: str, target: sqlalchemy.engine.Engine) -> None:
	"""Refuse to write into a used database, and only then bring the target up to schema.

	**The order is the whole of it, and it was the other way round** (`#306`). Migrating first
	meant that naming somebody else's instance with ``--to`` moved *their* schema forward
	through every intervening revision and then refused, reporting that nothing had happened.
	There is no downgrade, so that is not recoverable, and the build serving it would no longer
	start. A command whose entire promise is "nothing is at risk while you check" has to
	establish that the target is unused before it touches it at all.

	Asking an unmigrated database for its row counts is safe and is what makes the order
	possible: :func:`_counts` reads the table list off the database and reports zero for a
	table that is not there yet, so an empty target and a target from an older release are both
	answerable without writing anything.

	**Migrated rather than built with ``create_all``.** A database this leaves behind has to
	be one ``subroutine db upgrade`` will accept later, and that means an ``alembic_version`` row
	saying which revision it is — which `create_all` does not write. It is the same reason
	§10.3 keeps the migration path and the model path separate everywhere else.
	"""

	occupied = [name for name, count in _counts(target).items() if count]

	if occupied:
		raise subroutine.errors.ValidationError(
			f"The target database already holds data, in: {', '.join(sorted(occupied))}.",
			hint=(
				"Copy into an empty database. Merging two instances is not this command, "
				"and doing it by accident would leave neither of them right."
			),
		)

	# **And nothing else's tables either** (`#927`'s M-20). ``_counts`` walks the tables *this*
	# schema declares, so a database holding somebody else's application read as empty — and
	# the migration that follows would then create Subroutine's tables inside it. That is the
	# same harm `#306` moved this check earlier to prevent, arriving through the door marked
	# *not one of ours*.
	strangers = sorted(
		set(sqlalchemy.inspect(target).get_table_names())
		- {table.name for table in _tables()}
		- {"alembic_version"}
	)

	if strangers:
		raise subroutine.errors.ValidationError(
			f"The target database holds tables this does not know about: "
			f"{', '.join(strangers)}.",
			hint=(
				"That looks like somebody else's database. Copy into an empty one — this "
				"would put an instance's tables alongside whatever is already in there."
			),
		)

	if not subroutine.db.migrate.is_up_to_date(target):
		subroutine.db.migrate.upgrade(target_url)


def _tables () -> list[sqlalchemy.Table]:
	"""Every table, parents before children.

	``sorted_tables`` orders by foreign-key dependency, which is exactly the order the rows
	have to be inserted in — and it is derived from the models rather than listed here, so a
	table added later is copied without anybody remembering this file.
	"""

	return list(subroutine.db.base.Base.metadata.sorted_tables)


def _counts (engine: sqlalchemy.engine.Engine) -> dict[str, int]:
	"""Return the row count of every table, or zero for one that is not there yet."""

	found: dict[str, int] = {}
	present = set(sqlalchemy.inspect(engine).get_table_names())

	with engine.connect() as connection:
		for table in _tables():
			if table.name not in present:
				found[table.name] = 0

				continue

			total = connection.execute(
				sqlalchemy.select(sqlalchemy.func.count()).select_from(table)
			).scalar_one()
			found[table.name] = int(total)

	return found


def _move (
	source: sqlalchemy.engine.Engine, target: sqlalchemy.engine.Engine
) -> dict[str, int]:
	"""Copy every table's rows, parents first, and return how many of each."""

	counts: dict[str, int] = {}

	with source.connect() as reading, target.begin() as writing:
		for table in _tables():
			counts[table.name] = _copy_table(table, reading, writing)

		_restart_sequences(target, writing)

	return counts


def _copy_table (
	table: sqlalchemy.Table,
	reading: sqlalchemy.Connection,
	writing: sqlalchemy.Connection,
) -> int:
	"""Copy one table in batches, and return how many rows moved.

	**Through the table object rather than raw SQL**, so every value passes through its
	column's type in both directions. That is what makes a UUID stored as bare hex on SQLite
	arrive as a native ``uuid`` on PostgreSQL, and a JSON blob arrive as ``jsonb``, without
	this file knowing anything about either.
	"""

	inwards = _points_at_itself(table)
	keys = [column.name for column in table.primary_key.columns]
	moved = 0
	deferred: list[dict[str, typing.Any]] = []

	result = reading.execution_options(stream_results=True).execute(
		sqlalchemy.select(table)
	)

	while batch := result.mappings().fetchmany(BATCH):
		rows = [dict(row) for row in batch]

		for row in rows:
			if any(row[name] is not None for name in inwards):
				deferred.append(
					{f"_{name}": row[name] for name in keys}
					| {name: row[name] for name in inwards}
				)

				for name in inwards:
					row[name] = None

		writing.execute(sqlalchemy.insert(table), rows)
		moved += len(batch)

	if deferred:
		writing.execute(
			sqlalchemy.update(table).where(
				*(
					table.columns[name] == sqlalchemy.bindparam(f"_{name}")
					for name in keys
				)
			),
			deferred,
		)

	return moved


def _points_at_itself (table: sqlalchemy.Table) -> list[str]:
	"""Return the columns of a table that reference the same table.

	**``sorted_tables`` orders tables and says nothing about rows** (`#927`'s M-20), and five
	of these point at *themselves*: a sub-task, a section of a document, a project inside a
	project, the person an agent answers to, a reply to a comment. Insert a child before its
	parent and the foreign key refuses it — so ``add``, ``add``, ``move --under`` was enough to
	make the SQLite-to-PostgreSQL migration this command exists for permanently impossible, on
	an installation that had done nothing unusual.

	**Written last rather than sorted first**, because sorting is only exact where the table
	carries a depth — ``task``, ``document`` and ``project`` do; ``user`` and ``comment`` do
	not, and an accountability chain is a tree with no such column. Inserting the reference as
	null and filling it in afterwards is one mechanism that needs no order at all, and it goes
	on streaming: only the rows that *have* a parent are held, as a pair of identifiers.
	"""

	return [
		column.name
		for column in table.columns
		if column.nullable and any(key.column.table is table for key in column.foreign_keys)
	]


def _restart_sequences (
	target: sqlalchemy.engine.Engine, writing: sqlalchemy.Connection
) -> None:
	"""Move PostgreSQL's sequences past the ids that were just inserted.

	**Without this the first write after a transfer fails on a duplicate key**, and does it
	on the new database, minutes after somebody was told the copy succeeded. Inserting an
	explicit value does not advance the sequence backing the column, so it is still at 1
	while the table holds a million rows.

	SQLite has nothing to do here: its ``INTEGER PRIMARY KEY`` derives the next row id from
	the table itself.
	"""

	if target.dialect.name != "postgresql":
		return

	for table in _tables():
		for column in table.primary_key.columns:
			if not isinstance(column.type, sqlalchemy.BigInteger | sqlalchemy.Integer):
				continue

			# `pg_get_serial_sequence` returns null for a column with no sequence, and
			# `setval` of null is an error — so the coalesce is what keeps this general
			# rather than a list of the one table that has one today.
			writing.execute(
				sqlalchemy.text(
					"SELECT setval(s.name, GREATEST(s.top, 1), s.top > 0) "
					"FROM (SELECT pg_get_serial_sequence(:table, :column) AS name, "
					f"COALESCE((SELECT MAX({column.name}) FROM {table.name}), 0) AS top) AS s "
					"WHERE s.name IS NOT NULL"
				),
				{"table": table.name, "column": column.name},
			)


def _verify (
	source: sqlalchemy.engine.Engine,
	target: sqlalchemy.engine.Engine,
	counts: dict[str, int],
) -> None:
	"""Read both databases back and refuse if they disagree.

	**Counted at the target rather than trusted from the insert.** A driver that reported a
	batch it did not write, or a constraint that dropped rows, both look like success from
	the writing side — and this is the one command whose failure mode is somebody deleting
	the original afterwards.
	"""

	before = _counts(source)
	after = _counts(target)
	differences = [
		f"{name}: {before[name]} to copy, {after[name]} arrived"
		for name in sorted(before)
		if before[name] != after[name]
	]

	if differences:
		raise subroutine.errors.SubroutineError(
			"The copy did not arrive intact: " + "; ".join(differences),
			hint="The source database is untouched. Nothing has been lost.",
		)

	mismatched = sorted(name for name in counts if counts[name] != before[name])

	if mismatched:
		raise subroutine.errors.SubroutineError(
			f"Wrote a different number of rows than the source holds, in: "
			f"{', '.join(mismatched)}.",
			hint="The source database is untouched. Nothing has been lost.",
		)


def unusable_target (target_url: str) -> str | None:
	"""Return why this URL cannot be copied into, or ``None`` if it can.

	Checked before anything is read, so a typo in a URL is reported in the second it is made
	rather than after a long scan of a database that was fine.
	"""

	try:
		url = sqlalchemy.engine.make_url(target_url)

	# Broad on purpose: `make_url` raises `ArgumentError`, `ValueError` and
	# `AttributeError` for different malformed inputs, and a typo in a URL must be a
	# sentence rather than a traceback whichever one it happened to hit.
	except Exception:
		return "That is not a database URL."

	if not url.get_backend_name():
		return "That URL names no database engine."

	return None


def summarise (copied: Copied) -> list[str]:
	"""Return the per-table report, biggest first, for a command to print."""

	lines = []

	for name, count in sorted(copied.counts.items(), key=lambda pair: (-pair[1], pair[0])):
		if count:
			lines.append(f"  {name}: {count:,}")

	return lines
