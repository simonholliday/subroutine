"""Merge one instance's workspace into another's, keeping ids, timestamps and attribution.

**A supervised one-off, not a feature** (`SR#1756`). Simon's instruction when Oli's
self-installed instance had to be folded into the shared Hyperfence one: *"this is a one-off
case for my onboarding. I do not suggest that we offer this as a promised function for other
users."* So there is no CLI command, nothing is documented for operators, and this refuses far
more than a product would.

Run it twice: once to read, once to write.

    python scripts/merge_instance.py --source sqlite:///oli.db --target <url> \
        --into <workspace> --project inbox=inbox --project notes=oli-notes
    python scripts/merge_instance.py ... --commit

**What is preserved, and why it can be.** Every id in this schema is a UUID — a link, a
mention, a parent, a recurrence template and a superseding document all reference one — so
ids are carried unchanged and every join survives without being rebuilt. ``task.path`` is
built from task ids rather than project ids, so the sub-task hierarchy survives even when
items are re-filed into different projects. Timestamps are plain columns, so a row written
here keeps the moment it was really created; nothing above the domain can do that, because
``created_at`` is on neither request model.

**What is rewritten**: ``workspace_id``; ``ref``, as ``old + offset``; ``project_id``, from a
map given on the command line; every column naming a user; the three vocabulary columns,
matched by *key*; and ``#N`` written in prose.

**An offset rather than a compaction**, so the mapping is a single addition that a person can
check in their head against any item. Refs are never reused, so the gaps cost nothing.

**Only a ``#N`` that names one of the source's own items is rewritten.** A number in somebody's
prose may be an issue in another tracker, which is §6.15's recorded hazard; the report says how
many were rewritten and lists every one left alone, because that list is where a mistake would
show.

**One transaction.** PostgreSQL rolls back, so this lands whole or not at all — unlike a
migration on SQLite, which `SR#1689` measured cannot be undone. Nothing here needs DDL.

**It refuses rather than assuming.** Schema heads must match; the source must hold exactly one
workspace; and every user, status, item type, link type and project must map, or the run stops
and names what did not. That is deliberate: the two facts this was designed around — one user,
no custom vocabulary — were beliefs rather than measurements when it was written.
"""

import argparse
import dataclasses
import sys
import typing
import uuid

import sqlalchemy

import subroutine.db.base
import subroutine.db.migrate
import subroutine.db.models
import subroutine.db.session
import subroutine.domain.mentions
import subroutine.domain.tags

#: Copied, in the order they are inserted. Self-references are wired afterwards, so this order
#: only has to satisfy foreign keys *between* tables.
CARRIED = (
	"tag",
	"task",
	"document",
	"task_tag",
	"document_tag",
	"comment",
	"link",
	"mention",
	"verification",
	"event",
)

#: Not copied, each for a stated reason, so that a table missing from ``CARRIED`` is a decision
#: rather than an oversight. :func:`_every_table_is_accounted_for` fails the run if a table
#: appears in neither.
NOT_CARRIED = {
	"instance": "one per installation, and the target already has its own identity",
	"workspace": "the target's workspace is the destination, not another row",
	"user": "mapped to accounts that already exist; making them here would fork the identity",
	"role": "the target's roles govern the target's workspace",
	"workspace_member": "who may reach the workspace is the operator's to grant, not a copy",
	"project": "mapped to projects that already exist, by the map given on the command line",
	"project_member": "follows the projects, which are the target's own",
	"status": "mapped by key onto the target's vocabulary",
	"item_type": "mapped by key onto the target's vocabulary",
	"link_type": "mapped by key onto the target's vocabulary",
	"api_token": "a credential for an instance being decommissioned",
	"calendar_feed": "the same, and its URL names a host that is going away",
	"web_session": "a browser session on a machine nobody will sign into again",
	"login_link": "a one-time link, expired or spent either way",
}

#: Where a user is named, per table. Every one is mapped, and an unmapped user stops the run —
#: a row attributed to nobody is worse than a refusal, because nothing afterwards would say so.
USER_COLUMNS = {
	"task": ("created_by", "updated_by", "assignee_id", "assigned_by_id", "claimed_by_id"),
	"document": ("created_by", "updated_by", "owner_id"),
	"comment": ("author_id",),
	"link": ("created_by",),
	"verification": ("created_by", "updated_by"),
	"event": ("actor_user_id",),
}

#: Where prose lives. These are the columns a ``#N`` can be written in, and the only ones
#: renumbered.
PROSE_COLUMNS = {
	"task": ("title", "description"),
	"document": ("title", "body"),
	"comment": ("body",),
	"tag": ("description",),
}

#: The columns holding a ref, which is the one identifier in this schema that is not a UUID and
#: therefore the one that can collide.
REF_COLUMNS = {"task": "ref", "document": "ref"}

#: Self-references, nulled on insert and wired in a second pass. Doing it this way means the
#: insert order inside a table never has to be worked out — a child before its parent, a task
#: before the template it came from.
#:
#: **A document's `supersedes_id` was here and is gone** (`SR#1684`): superseding is a link now,
#: and a link is an ordinary row with no self-reference to unpick.
SELF_REFERENCES = {
	"task": ("parent_task_id", "recurrence_template_id"),
	"document": ("parent_id",),
}

#: Vocabulary, as (table it is written in, column, table it points at).
VOCABULARY = (
	("task", "status_id", "status"),
	("task", "type_id", "item_type"),
	("document", "status_id", "status"),
	("document", "type_id", "item_type"),
	("link", "link_type_id", "link_type"),
)


@dataclasses.dataclass
class Report:
	"""What the run did, or would do. Printed whether or not anything is written."""

	offset: int
	refs: dict[int, int] = dataclasses.field(default_factory=dict)
	counts: dict[str, int] = dataclasses.field(default_factory=dict)
	tags_made: list[str] = dataclasses.field(default_factory=list)
	rewritten: int = 0
	left_alone: dict[int, int] = dataclasses.field(default_factory=dict)
	samples: list[tuple[str, str]] = dataclasses.field(default_factory=list)


class Refused (Exception):
	"""Something did not map, or the two databases cannot be merged as they stand."""


def _table (name: str) -> sqlalchemy.Table:
	"""Return the model's table by name."""

	return subroutine.db.base.Base.metadata.tables[name]


def _every_table_is_accounted_for () -> None:
	"""Fail unless every table in the schema is either carried or refused in writing."""

	known = set(CARRIED) | set(NOT_CARRIED)
	tables = set(subroutine.db.base.Base.metadata.tables)

	missing = sorted(tables - known)

	if missing:
		raise Refused(
			f"the schema has grown {missing}, which this script neither copies nor refuses. "
			f"Add it to CARRIED or to NOT_CARRIED with a reason."
		)


def _heads_agree (source: sqlalchemy.Engine, target: sqlalchemy.Engine) -> str:
	"""Return the revision both databases are at, refusing unless they are at the same one."""

	here = subroutine.db.migrate.current_revision(source)
	there = subroutine.db.migrate.current_revision(target)

	if here != there:
		raise Refused(
			f"the source is at schema {here} and the target at {there}. Bring the older one up "
			f"with 'subroutine db upgrade' before merging — a column that exists on one side "
			f"and not the other would be dropped silently."
		)

	if here is None:
		raise Refused("neither database has been migrated, so there is no schema to merge")

	return here


def _one_workspace (connection: sqlalchemy.Connection) -> uuid.UUID:
	"""Return the source's only workspace, refusing if it holds more than one."""

	workspace = _table("workspace")
	rows = connection.execute(
		sqlalchemy.select(workspace.c.id, workspace.c.slug).where(workspace.c.deleted_at.is_(None))
	).all()

	if len(rows) != 1:
		named = ", ".join(str(row.slug) for row in rows) or "none"
		raise Refused(
			f"the source holds {len(rows)} workspaces ({named}) and this merges one. Say which "
			f"by deleting the others from a copy, or merge them one at a time."
		)

	return typing.cast(uuid.UUID, rows[0].id)


def _named_workspace (connection: sqlalchemy.Connection, slug: str) -> uuid.UUID:
	"""Return the target workspace with this slug."""

	workspace = _table("workspace")
	found = connection.execute(
		sqlalchemy.select(workspace.c.id).where(
			workspace.c.slug == slug, workspace.c.deleted_at.is_(None)
		)
	).scalar_one_or_none()

	if found is None:
		raise Refused(f"the target has no workspace called {slug!r}")

	return typing.cast(uuid.UUID, found)


def _not_already_merged (
	source: sqlalchemy.Connection, target: sqlalchemy.Connection
) -> None:
	"""Refuse if any of the source's items is already in the target.

	Running this twice is **safe** — the second run fails on a duplicate primary key and the
	transaction rolls back, which was driven rather than assumed. What it is not is *legible*:
	the failure is several screens of SQL with every column of every row in it, met by somebody
	who is not sure whether the first run worked. One sentence is worth more than that on the
	day, and this is the question they are actually asking.
	"""

	for name in ("task", "document"):
		table = _table(name)
		ids = list(source.execute(sqlalchemy.select(table.c.id)).scalars())

		if not ids:
			continue

		already = target.execute(
			sqlalchemy.select(sqlalchemy.func.count())
			.select_from(table)
			.where(table.c.id.in_(ids))
		).scalar_one()

		if already:
			raise Refused(
				f"{already} of the source's {name}s are already in the target, so this has been "
				f"merged before. Nothing has been changed. If the first run was incomplete, "
				f"restore the backup taken before it and start again."
			)


def _user_map (
	source: sqlalchemy.Connection, target: sqlalchemy.Connection, given: dict[str, str]
) -> dict[uuid.UUID, uuid.UUID]:
	"""Match every account that wrote anything in the source to one in the target.

	By normalised username unless the command line says otherwise, and **an account with no
	match stops the run**. Attribution is the reason a person hands over work they would
	otherwise supervise (`SR#1414`), and a row quietly reattributed says nothing about having
	been.
	"""

	user = _table("user")
	theirs = source.execute(
		sqlalchemy.select(user.c.id, user.c.username, user.c.username_normalized)
	).all()
	ours = {
		row.username_normalized: row.id
		for row in target.execute(sqlalchemy.select(user.c.id, user.c.username_normalized))
	}

	mapped: dict[uuid.UUID, uuid.UUID] = {}
	unmatched: list[str] = []

	for row in theirs:
		wanted = given.get(str(row.username), str(row.username))
		landing = ours.get(subroutine.domain.tags.normalize(wanted))

		if landing is None:
			unmatched.append(f"{row.username} (looked for {wanted!r})")

			continue

		mapped[row.id] = landing

	if unmatched:
		raise Refused(
			f"these accounts wrote in the source and have no match in the target: "
			f"{', '.join(unmatched)}. Create them, or say where each one lands with "
			f"--user <theirs>=<ours>."
		)

	return mapped


def _keyed (
	connection: sqlalchemy.Connection, name: str, workspace: uuid.UUID
) -> dict[tuple[typing.Any, ...], uuid.UUID]:
	"""Return one workspace's vocabulary of this kind, by the key that identifies a row.

	At module level rather than inside the loop that calls it, because a function defined in a
	loop closes over the *variable* and not its value — so a scan over three tables would build
	all three from whichever one the loop finished on. Ruff refuses it (B023) and was right.
	"""

	table = _table(name)
	keyed = [table.c.key]

	if "entity_type" in table.columns:
		keyed.insert(0, table.c.entity_type)

	rows = connection.execute(
		sqlalchemy.select(table.c.id, *keyed).where(table.c.workspace_id == workspace)
	).all()

	return {tuple(row[1:]): row.id for row in rows}


def _vocabulary_map (
	source: sqlalchemy.Connection, target: sqlalchemy.Connection,
	theirs: uuid.UUID, ours: uuid.UUID,
) -> dict[uuid.UUID, uuid.UUID]:
	"""Match the source's statuses, item types and link types onto the target's, by key.

	**By key rather than by name**, because a workspace renames what it calls things (§5.5) and
	the key is what every rule reads. A key the target does not have stops the run: inventing
	one changes the target's vocabulary, which is a decision rather than an import.
	"""

	mapped: dict[uuid.UUID, uuid.UUID] = {}
	missing: list[str] = []

	for name in ("status", "item_type", "link_type"):
		here = _keyed(source, name, theirs)
		there = _keyed(target, name, ours)

		for key, identifier in here.items():
			landing = there.get(key)

			if landing is None:
				missing.append(f"{name} {'/'.join(str(part) for part in key)}")

				continue

			mapped[identifier] = landing

	if missing:
		raise Refused(
			f"the target workspace does not have {', '.join(missing)}. Add them there first — "
			f"creating vocabulary during an import changes what the target means by a word."
		)

	return mapped


def _project_map (
	source: sqlalchemy.Connection, target: sqlalchemy.Connection,
	theirs: uuid.UUID, ours: uuid.UUID, given: dict[str, str],
) -> dict[uuid.UUID, uuid.UUID]:
	"""Match each source project onto a target project, from the map given on the command line.

	Only projects that actually hold something have to be mapped, so an empty one somebody made
	and never used does not have to be recreated to satisfy this.
	"""

	project = _table("project")
	task = _table("task")
	document = _table("document")

	used = set(source.execute(sqlalchemy.select(task.c.project_id).distinct()).scalars())
	used |= set(source.execute(sqlalchemy.select(document.c.project_id).distinct()).scalars())
	used.discard(None)

	theirs_by_id = {
		row.id: row.key
		for row in source.execute(
			sqlalchemy.select(project.c.id, project.c.key).where(
				project.c.workspace_id == theirs
			)
		)
	}
	ours_by_key = {
		row.key: row.id
		for row in target.execute(
			sqlalchemy.select(project.c.id, project.c.key).where(project.c.workspace_id == ours)
		)
	}

	mapped: dict[uuid.UUID, uuid.UUID] = {}
	missing: list[str] = []

	for identifier in sorted(used, key=str):
		key = theirs_by_id.get(identifier)

		if key is None:
			missing.append(f"a project not in the source workspace ({identifier})")

			continue

		landing = ours_by_key.get(given.get(str(key), str(key)))

		if landing is None:
			missing.append(f"{key} -> {given.get(str(key), str(key))!r}, which the target has not")

			continue

		mapped[identifier] = landing

	if missing:
		raise Refused(
			f"these projects hold items and do not map: {'; '.join(missing)}. Make the target "
			f"project, or say where it lands with --project <theirs>=<ours>."
		)

	return mapped


def _tag_map (
	source: sqlalchemy.Connection, target: sqlalchemy.Connection,
	theirs: uuid.UUID, ours: uuid.UUID, report: Report,
) -> tuple[dict[uuid.UUID, uuid.UUID], set[uuid.UUID]]:
	"""Match tags by normalised name, and say which ones the target does not have yet.

	Unlike vocabulary, a missing tag is *made* rather than refused: a tag is a word somebody
	typed rather than a term the workspace has agreed, so carrying one in changes nothing about
	what the target means.
	"""

	tag = _table("tag")
	here = source.execute(
		sqlalchemy.select(tag).where(tag.c.workspace_id == theirs)
	).mappings().all()
	there = {
		row.name_normalized: row.id
		for row in target.execute(
			sqlalchemy.select(tag.c.id, tag.c.name_normalized).where(tag.c.workspace_id == ours)
		)
	}

	mapped: dict[uuid.UUID, uuid.UUID] = {}
	made: set[uuid.UUID] = set()

	for row in here:
		landing = there.get(row["name_normalized"])

		if landing is None:
			# **Carried under its own id, so it maps to itself.** The map has to be total —
			# `task_tag` looks every tag up in it, and a tag the target had not met would
			# otherwise be the one row with nothing to point at.
			report.tags_made.append(str(row["name"]))
			made.add(row["id"])
			mapped[row["id"]] = row["id"]

			continue

		mapped[row["id"]] = landing

	return mapped, made


def _renumbered (
	text: str | None, refs: dict[int, int], report: Report
) -> str | None:
	"""Return prose with every ``#N`` that names a source item renumbered, and nothing else.

	**A number that names nothing here is left exactly as it was.** §6.15 records why: a `#42`
	in somebody's writing may be an issue in another tracker, and this cannot tell the two apart
	except by asking whether it resolves. Every one left alone is counted and reported, because
	that count is where a wrong answer would show.
	"""

	if not text:
		return text

	def swap (match: typing.Match[str]) -> str:
		"""Return what this one ``#N`` becomes, counting both answers as it goes."""

		old = int(match.group(1))
		landing = refs.get(old)

		if landing is None:
			report.left_alone[old] = report.left_alone.get(old, 0) + 1

			return match.group(0)

		report.rewritten += 1

		return f"#{landing}"

	return subroutine.domain.mentions.REF_PATTERN.sub(swap, text)


def _renumbered_inside (value: typing.Any, refs: dict[int, int], report: Report) -> typing.Any:
	"""Walk a stored JSON value and renumber the prose in it.

	This is for ``event.changes``, which holds the before and after of a field whole. Leaving it
	alone would keep the bytes somebody really wrote and make the history point at other
	people's items; rewriting keeps what it *meant*. The bytes are already not what they were,
	because every ref in the instance has moved.
	"""

	if isinstance(value, str):
		return _renumbered(value, refs, report)

	if isinstance(value, list):
		return [_renumbered_inside(one, refs, report) for one in value]

	if isinstance(value, dict):
		return {key: _renumbered_inside(one, refs, report) for key, one in value.items()}

	return value


def _ref_map (source: sqlalchemy.Connection, offset: int) -> dict[int, int]:
	"""Return every source ref and the number it becomes.

	Tasks and documents share one counter per workspace (§6.2), so they are numbered together
	here for the same reason.
	"""

	refs: dict[int, int] = {}

	for name, column in REF_COLUMNS.items():
		table = _table(name)

		for ref in source.execute(sqlalchemy.select(table.c[column])).scalars():
			refs[int(ref)] = int(ref) + offset

	return refs


@dataclasses.dataclass(frozen=True)
class Maps:
	"""Everything a row is rewritten with, resolved once before anything is written."""

	workspace: uuid.UUID
	users: dict[uuid.UUID, uuid.UUID]
	vocabulary: dict[uuid.UUID, uuid.UUID]
	projects: dict[uuid.UUID, uuid.UUID]
	tags: dict[uuid.UUID, uuid.UUID]
	tags_made: frozenset[uuid.UUID]
	refs: dict[int, int]
	offset: int
	seq_offset: int


def _rewrite (
	name: str, row: dict[str, typing.Any], maps: Maps, report: Report
) -> dict[str, typing.Any]:
	"""Return one row as it will be written into the target."""

	out = dict(row)

	if "workspace_id" in out:
		out["workspace_id"] = maps.workspace

	if name in REF_COLUMNS:
		out[REF_COLUMNS[name]] = maps.refs[int(out[REF_COLUMNS[name]])]

	if "project_id" in out and out["project_id"] is not None:
		out["project_id"] = maps.projects[out["project_id"]]

	for column in USER_COLUMNS.get(name, ()):
		if out.get(column) is not None:
			out[column] = maps.users[out[column]]

	for table, column, _points_at in VOCABULARY:
		if table == name and out.get(column) is not None:
			out[column] = maps.vocabulary[out[column]]

	if name in ("task_tag", "document_tag"):
		out["tag_id"] = maps.tags[out["tag_id"]]

	if name == "tag":
		out["id"] = maps.tags.get(out["id"], out["id"])

	for column in PROSE_COLUMNS.get(name, ()):
		out[column] = _renumbered(out.get(column), maps.refs, report)

	if name == "event":
		out["seq"] = int(out["seq"]) + maps.seq_offset
		out["changes"] = _renumbered_inside(out.get("changes"), maps.refs, report)

	for column in SELF_REFERENCES.get(name, ()):
		out[column] = None

	return out


def _carry (
	source: sqlalchemy.Connection, writing: sqlalchemy.Connection, maps: Maps, report: Report,
	*, commit: bool,
) -> None:
	"""Read every carried table, rewrite each row, and write it into the target."""

	for name in CARRIED:
		table = _table(name)
		rows = source.execute(sqlalchemy.select(table)).mappings().all()
		written = []

		for row in rows:
			if name == "tag" and row["id"] not in maps.tags_made:
				# Already there under the same normalised name, so the target's row wins and
				# `task_tag` points at it rather than a second row spelling the same word.
				continue

			written.append(_rewrite(name, dict(row), maps, report))

		report.counts[name] = len(written)

		if written and commit:
			writing.execute(sqlalchemy.insert(table), written)


def _wire_self_references (
	source: sqlalchemy.Connection, writing: sqlalchemy.Connection, maps: Maps,
) -> None:
	"""Set the columns that were nulled on insert, now that every row is there.

	Nulled first so the order rows go in never has to be worked out — a child before its parent,
	a task before the template that mints it. The ids are unchanged, so this is the source's own
	values written back.
	"""

	for name, columns in SELF_REFERENCES.items():
		table = _table(name)
		wanted = [table.c.id, *(table.c[column] for column in columns)]

		for row in source.execute(sqlalchemy.select(*wanted)).mappings():
			values = {column: row[column] for column in columns if row[column] is not None}

			if not values:
				continue

			writing.execute(
				sqlalchemy.update(table).where(table.c.id == row["id"]).values(**values)
			)


def _advance_the_counter (
	writing: sqlalchemy.Connection, workspace: uuid.UUID, refs: dict[int, int]
) -> int:
	"""Move the target workspace's ref counter past everything just written."""

	table = _table("workspace")
	after = max(refs.values()) + 1 if refs else 0
	current = writing.execute(
		sqlalchemy.select(table.c.next_ref_number).where(table.c.id == workspace)
	).scalar_one()

	if after <= current:
		return int(current)

	writing.execute(
		sqlalchemy.update(table).where(table.c.id == workspace).values(next_ref_number=after)
	)

	return after


def _restart_sequences (writing: sqlalchemy.Connection) -> None:
	"""Move PostgreSQL's sequences past the ids just inserted.

	``event.seq`` is written explicitly here, and inserting an explicit value does not advance
	the sequence behind the column — so without this the next event written on the target fails
	on a duplicate key, minutes after somebody was told the merge succeeded. Lifted from
	``db/transfer.py``, which learnt it the same way.
	"""

	if writing.dialect.name != "postgresql":
		return

	writing.execute(
		sqlalchemy.text(
			"SELECT setval(s.name, GREATEST(s.top, 1), s.top > 0) "
			"FROM (SELECT pg_get_serial_sequence('event', 'seq') AS name, "
			"COALESCE((SELECT MAX(seq) FROM event), 0) AS top) AS s WHERE s.name IS NOT NULL"
		)
	)


def _verify (writing: sqlalchemy.Connection, maps: Maps, report: Report) -> list[str]:
	"""Check what landed, and return anything wrong with it."""

	wrong = []

	for name, column in REF_COLUMNS.items():
		table = _table(name)
		duplicates = writing.execute(
			sqlalchemy.select(table.c[column])
			.where(table.c.workspace_id == maps.workspace)
			.group_by(table.c[column])
			.having(sqlalchemy.func.count() > 1)
		).scalars().all()

		if duplicates:
			wrong.append(f"{name} has two rows sharing refs {sorted(duplicates)[:10]}")

	both = _table("task"), _table("document")
	shared = writing.execute(
		sqlalchemy.select(both[0].c.ref)
		.where(both[0].c.workspace_id == maps.workspace)
		.intersect(
			sqlalchemy.select(both[1].c.ref).where(both[1].c.workspace_id == maps.workspace)
		)
	).scalars().all()

	if shared:
		wrong.append(f"a task and a document share refs {sorted(shared)[:10]}")

	for name in CARRIED:
		table = _table(name)

		if "workspace_id" not in table.columns:
			continue

		here = writing.execute(
			sqlalchemy.select(sqlalchemy.func.count())
			.select_from(table)
			.where(table.c.workspace_id == maps.workspace)
		).scalar_one()

		if here < report.counts.get(name, 0):
			wrong.append(f"{name} holds {here} rows and {report.counts[name]} were written")

	return wrong


def _said (report: Report, maps: Maps, landed: list[str]) -> None:
	"""Print what happened, or what would."""

	print(f"  refs        {report.offset:+d}, so #1 becomes #{1 + report.offset}")

	if report.refs:
		lowest, highest = min(report.refs), max(report.refs)

		print(
			f"              {len(report.refs)} items, #{lowest}-#{highest} "
			f"-> #{lowest + report.offset}-#{highest + report.offset}"
		)

	print(f"  accounts    {len(maps.users)} mapped")
	print(f"  vocabulary  {len(maps.vocabulary)} mapped")
	print(f"  projects    {len(maps.projects)} mapped")
	# The map is total — a tag the target has not met maps to itself — so what was matched
	# is the map less the ones being carried, rather than the map.
	print(
		f"  tags        {len(maps.tags) - len(report.tags_made)} matched, "
		f"{len(report.tags_made)} to make"
	)

	if report.tags_made:
		print(f"              new: {', '.join(sorted(report.tags_made))}")

	print()

	for name in CARRIED:
		print(f"  {name:<14}{report.counts.get(name, 0)}")

	print()
	print(f"  prose       {report.rewritten} refs renumbered")

	if report.left_alone:
		total = sum(report.left_alone.values())
		named = ", ".join(f"#{ref}x{count}" for ref, count in sorted(report.left_alone.items()))

		print(f"              {total} left alone, naming nothing here: {named}")

	for title, after in report.samples:
		print(f"              {title!r} -> {after!r}")

	if landed:
		print()

		for problem in landed:
			print(f"  WRONG       {problem}")


def _pairs (given: list[str] | None) -> dict[str, str]:
	"""Read ``a=b`` arguments into a map."""

	pairs = {}

	for one in given or ():
		if "=" not in one:
			raise Refused(f"{one!r} is not a map — write it as <theirs>=<ours>")

		theirs, ours = one.split("=", 1)
		pairs[theirs.strip()] = ours.strip()

	return pairs


def merge (
	source_url: str, target_url: str, into: str, *,
	projects: dict[str, str], users: dict[str, str], commit: bool,
) -> tuple[Report, Maps, list[str]]:
	"""Merge the source's only workspace into the named workspace of the target."""

	_every_table_is_accounted_for()

	reading = subroutine.db.session.create_engine(source_url)
	writing = subroutine.db.session.create_engine(target_url)

	try:
		revision = _heads_agree(reading, writing)

		print(f"  schema      both at {revision}")

		with reading.connect() as source, writing.connect() as target:
			theirs = _one_workspace(source)
			ours = _named_workspace(target, into)

			_not_already_merged(source, target)

			counter = target.execute(
				sqlalchemy.select(_table("workspace").c.next_ref_number).where(
					_table("workspace").c.id == ours
				)
			).scalar_one()

			offset = int(counter) - 1
			report = Report(offset=offset)
			report.refs = _ref_map(source, offset)
			tags, tags_made = _tag_map(source, target, theirs, ours, report)

			maps = Maps(
				workspace=ours,
				users=_user_map(source, target, users),
				vocabulary=_vocabulary_map(source, target, theirs, ours),
				projects=_project_map(source, target, theirs, ours, projects),
				tags=tags,
				tags_made=frozenset(tags_made),
				refs=report.refs,
				offset=offset,
				seq_offset=int(
					target.execute(
						sqlalchemy.select(sqlalchemy.func.coalesce(
							sqlalchemy.func.max(_table("event").c.seq), 0
						))
					).scalar_one()
				),
			)

			# **The whole write is one transaction**, so a refusal anywhere leaves the target
			# exactly as it was. PostgreSQL really does roll this back, which is what makes a
			# merge safe to attempt at all — `SR#1689` measured the case where that is untrue.
			#
			# No `begin()` here: reading the maps above has already opened the transaction this
			# connection is in, and every write below joins it. Asking for another is what
			# SQLAlchemy refuses, and the first version of this did.
			_carry(source, target, maps, report, commit=commit)

			landed: list[str] = []

			if commit:
				_wire_self_references(source, target, maps)
				_advance_the_counter(target, ours, report.refs)
				_restart_sequences(target)

				landed = _verify(target, maps, report)

			if landed or not commit:
				target.rollback()

			else:
				target.commit()

			if landed:
				raise Refused(
					"the merge did not land cleanly and has been rolled back: "
					+ "; ".join(landed)
				)

		return report, maps, landed

	finally:
		reading.dispose()
		writing.dispose()


def main (argv: list[str] | None = None) -> int:
	"""Read the arguments, merge, and say what happened."""

	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])

	parser.add_argument("--source", required=True, help="the database being merged in")
	parser.add_argument("--target", required=True, help="the database it lands in")
	parser.add_argument("--into", required=True, help="the target workspace's short name")
	parser.add_argument(
		"--project", action="append", metavar="THEIRS=OURS",
		help="where a source project lands; repeatable",
	)
	parser.add_argument(
		"--user", action="append", metavar="THEIRS=OURS",
		help="where a source account lands, if the username differs; repeatable",
	)
	parser.add_argument(
		"--commit", action="store_true",
		help="write it. Without this nothing is kept and the report says what would happen",
	)

	arguments = parser.parse_args(argv)

	try:
		report, maps, landed = merge(
			arguments.source, arguments.target, arguments.into,
			projects=_pairs(arguments.project), users=_pairs(arguments.user),
			commit=arguments.commit,
		)

	except Refused as refusal:
		print(f"Refused: {refusal}", file=sys.stderr)

		return 1

	_said(report, maps, landed)

	print()
	print(
		f"{'Merged' if arguments.commit else 'Would merge'} "
		f"{sum(report.counts.values())} rows into {arguments.into}."
	)

	if not arguments.commit:
		print("Nothing was written. Add --commit to do it.")

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
