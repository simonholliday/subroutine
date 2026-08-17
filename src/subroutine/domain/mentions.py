"""Finding the references people write in prose, and keeping an index of them.

docs/design.md §6.15. A task refers to a specification, a comment cites a decision. That is not
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

#: A reference in running text: ``#42`` (docs/design.md §6.15). It is only ever a *candidate* —
#: a ref becomes a mention when it resolves — but the pattern still has to be tight,
#: because ``#`` is a busy character:
#:
#: * **No word character either side.** The trailing guard is what keeps the hex colour
#:   ``#42FF00`` out of the index; the leading one rejects ``rgb#42`` and ``##42``.
#:   ``\w`` rather than an ASCII class, so ``#12ème`` is prose in French too.
#: * **No leading zero.** ``#007`` is a Bond film, not ref 7. Refs are minted without
#:   padding, so a padded number is somebody writing about something else.
#: * **Lookarounds, never ``\b``.** ``\b`` sits between a letter and an apostrophe, which
#:   is how ``\btomorrow\b`` came to match inside ``tomorrow's`` in ``domain.capture``
#:   and mangle a title. The same mistake here would index the wrong item.
#:
#: ``#42`` cannot open a markdown heading — CommonMark requires a space after the ``#``
#: run — and cannot be a ``#tag`` from quick capture, which must begin with a letter.
REF_PATTERN = re.compile(r"(?<![\w#])#([1-9][0-9]*)(?!\w)")

#: The explicit form, ``[label](subroutine:42)``. It carries no sigil, so the pattern
#: above cannot see it and this one is not a convenience.
LINK_PATTERN = re.compile(r"subroutine:#?([1-9][0-9]*)(?![\w/-])")

#: A reference to another workspace on this instance, ``subroutine:acme/42``. It has to be
#: recognised in order to be *ignored*: the ref inside it names a workspace this index does
#: not cover, and resolving it locally would point at whatever happens to share the number
#: here. Scrubbed before either pattern above runs.
FOREIGN_LINK_PATTERN = re.compile(r"subroutine:[a-z0-9][a-z0-9-]*/#?[0-9]+")

#: How many distinct references are indexed from one source. A 256 KiB body full of
#: ref-shaped text is a plausible accident, and an unbounded write amplification on every
#: save is not a thing to discover in production. Earlier references win, so the cap is
#: deterministic rather than dependent on set ordering.
MAX_MENTIONS_PER_SOURCE = 100


def candidates (*texts: str | None) -> list[int]:
	"""Return the refs written across some pieces of text, in order of first appearance.

	Both spellings are collected: the bare ``#42`` and the explicit
	``[label](subroutine:42)``. A cross-workspace link is deliberately skipped — it names
	a workspace this index does not cover, and resolving it locally would silently point
	at whatever happens to share the number here.
	"""

	found: list[int] = []
	seen: set[int] = set()

	for text in texts:
		if not text:
			continue

		local = FOREIGN_LINK_PATTERN.sub(" ", text)

		# Ordered by where each reference appears rather than by which spelling found it,
		# so "in order of first appearance" is true of the text and not of this function.
		matches = sorted(
			(*REF_PATTERN.finditer(local), *LINK_PATTERN.finditer(local)),
			key=lambda match: match.start(),
		)

		for match in matches:
			ref = int(match.group(1))

			if ref in seen:
				continue

			seen.add(ref)
			found.append(ref)

	return found


def resolve (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, refs: typing.Sequence[int]
) -> dict[int, tuple[str, uuid.UUID]]:
	"""Look up which refs name something here, as ``{ref: (entity_type, id)}``.

	Refs that name nothing are simply absent, and stay plain text. Two queries rather than
	one per ref, because a long description can carry a great many.
	"""

	if not refs:
		return {}

	wanted = set(refs)
	found: dict[int, tuple[str, uuid.UUID]] = {}

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
	"""Return everything whose prose refers to one item.

	**Narrowed by workspace and nothing else, and that is not yet sufficient.** §6.15 says a
	mention from a project the reader cannot see is *omitted entirely* — not reported as
	invisible the way a cross-boundary link is, because "something you cannot see mentioned
	this" discloses that activity exists and explains nothing. This function does not do
	that, which is safe only because nothing calls it: it is here ahead of
	``include=backlinks`` (M3).

	So: **whoever wires this to an endpoint owes the project-visibility narrowing**, through
	``domain.scoping``, before it returns anything to a caller. Written down here rather than
	left to be noticed, because an unnarrowed read path that already looks finished is how
	the agenda came to ignore ``project_scope``.
	"""

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
