"""Finding the references people write in prose, and keeping an index of them.

SPEC.md §6.15. A task refers to a specification, a comment cites a decision. That is not
§5.7's link — nobody asserted a typed relationship, they wrote a sentence — but "what
refers to this?" is still the most valuable question a knowledge system answers, and it
cannot be answered by scanning descriptions at query time.

So the prose stays the source of truth and this keeps a derived index beside it. Bodies
are never altered, never rendered, and never parsed as markdown: this reads them and
writes rows elsewhere. If every mention were extracted wrongly the text would still be
exactly what its author typed, which is the test of whether that separation is real.
"""

import re
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.work
import subroutine.domain.refs

#: A bare ref in running text. Project keys are short and uppercase, so this is specific
#: enough to be worth resolving and loose enough to catch what people actually write. It
#: is only ever a *candidate*: a ref becomes a mention when it resolves, which is what
#: keeps "the SR-71 Blackbird" and "IR-35 applies here" as prose.
REF_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]{0,15})-(\d+)\b")

#: A reference to another workspace on this instance, ``subroutine:acme/SR-42``. It has to
#: be recognised in order to be *ignored*: the ref inside it would otherwise be found by
#: the pattern above and resolved against whatever happens to answer to it here.
#:
#: The local explicit form, ``[label](subroutine:SR-42)``, needs no handling at all — the
#: pattern above already finds the ref inside it.
FOREIGN_LINK_PATTERN = re.compile(r"subroutine:[a-z0-9][a-z0-9-]*/[A-Z][A-Z0-9]{0,15}-\d+")

#: How many distinct references are indexed from one source. A 256 KiB body full of
#: ref-shaped text is a plausible accident, and an unbounded write amplification on every
#: save is not a thing to discover in production. Earlier references win, so the cap is
#: deterministic rather than dependent on set ordering.
MAX_MENTIONS_PER_SOURCE = 100


def candidates (*texts: str | None) -> list[str]:
	"""Return the refs written across some pieces of text, in order of first appearance.

	Both spellings are collected: the bare ``SR-42`` and the explicit
	``[label](subroutine:SR-42)``. A cross-workspace link is deliberately skipped — it
	names a workspace this index does not cover, and resolving it locally would silently
	point at whatever happens to share the ref here.
	"""

	found: list[str] = []
	seen: set[str] = set()

	for text in texts:
		if not text:
			continue

		local = FOREIGN_LINK_PATTERN.sub(" ", text)

		for match in REF_PATTERN.finditer(local):
			ref = match.group(0)

			if ref in seen:
				continue

			seen.add(ref)
			found.append(ref)

	return found


def resolve (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, refs: typing.Sequence[str]
) -> dict[str, tuple[str, uuid.UUID]]:
	"""Look up which refs name something here, as ``{ref: (entity_type, id)}``.

	Refs that name nothing are simply absent, and stay plain text. Two queries rather than
	one per ref, because a long description can carry a great many.
	"""

	if not refs:
		return {}

	wanted = set(refs)
	found: dict[str, tuple[str, uuid.UUID]] = {}

	models: tuple[tuple[str, typing.Any], ...] = (
		("task", subroutine.db.models.work.Task),
		("document", subroutine.db.models.work.Document),
	)

	for entity_type, model in models:
		rows = session.execute(
			sqlalchemy.select(model.ref, model.id).where(
				model.workspace_id == workspace_id, model.ref.in_(wanted)
			)
		).tuples()

		for ref, identifier in rows:
			# A ref names exactly one thing (§6.2), but the schema enforces that per table
			# rather than across both. If the allocator ever slipped, the task wins and
			# the behaviour is at least documented and deterministic.
			found.setdefault(ref, (entity_type, identifier))

	return {ref: found[ref] for ref in refs if ref in found}


def synchronize (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	source_type: str,
	source_id: uuid.UUID,
	texts: typing.Sequence[str | None],
) -> int:
	"""Rewrite one source's mentions from its current text, returning how many there are.

	Replaced wholesale rather than diffed. The text is the truth, so working out which
	rows to add and remove is effort spent reproducing a state we can simply reassert —
	and a diff that drifts leaves a backlink pointing at a sentence that no longer says it.
	"""

	model = subroutine.db.models.work.Mention

	session.execute(
		sqlalchemy.delete(model)
		.where(model.source_type == source_type, model.source_id == source_id)
		.execution_options(synchronize_session=False)
	)

	targets = resolve(session, workspace_id, candidates(*texts))
	written = 0

	for entity_type, identifier in targets.values():
		if entity_type == source_type and identifier == source_id:
			continue

		if written >= MAX_MENTIONS_PER_SOURCE:
			break

		session.add(
			model(
				workspace_id=workspace_id,
				source_type=source_type,
				source_id=source_id,
				target_type=entity_type,
				target_id=identifier,
			)
		)
		written += 1

	return written


def backlinks (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	target_type: str,
	target_id: uuid.UUID,
) -> list[subroutine.db.models.work.Mention]:
	"""Return everything whose prose refers to one item."""

	model = subroutine.db.models.work.Mention

	return list(
		session.scalars(
			sqlalchemy.select(model)
			.where(
				model.workspace_id == workspace_id,
				model.target_type == target_type,
				model.target_id == target_id,
			)
			.order_by(model.created_at)
		)
	)
