"""What happened over a period, as against what changed — item `#1430`, decision `#1429`.

**One store, two reads.** :mod:`subroutine.domain.events` answers *what changed*: raw, cheap,
complete, resumable from a cursor, and exactly what a client polling for work wants. This
answers *what happened*, which is a different question asked by a person or by an agent told to
say what a stretch of time contained — and the difference between the two is entirely a
**join**.

**Measured before it was designed**, on one day of real work: 450 events, of which 130 were
``comment.created`` carrying no body at all, 51 field-changes whose values were bare UUIDs, and
an actor column that was a UUID on every single row. So the feed had the skeleton — ``seq``, an
order, and a title on every row — and none of the substance.

**Nothing is written differently.** The obvious fix is to put the comment's body on the event,
and it is refused: ``event.changes`` already stores ``from`` and ``to`` in full rather than as a
diff, in a table that is never pruned, which is a filed bug at `#578`. Copying bodies in would
compound it on 29% of the feed. The events already carry the ids; a second reader joins.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity

#: The lookups a value can be named by. Strings rather than an enum because they are keys into
#: :class:`subroutine.views.Vocabulary`, which is where the batch loading already lives.
USER = "user"
STATUS = "status"
TYPE = "type"
PROJECT = "project"
TASK = "task"

#: Which lookup names the value of an id-valued change, by the column it moved.
#:
#: **Declared rather than inferred, because a name cannot answer it.** A column ending ``_id``
#: says nothing about which table it points at, and guessing from the stem would put
#: ``recurrence_template_id`` at a ``recurrence_template`` table that does not exist.
#:
#: **What is missing degrades to silence rather than to a UUID**, which is the design decision
#: worth stating: a column absent from here renders its phrase and no value — *changed how it
#: repeats* — where a UUID would be noise a reader has to learn to skip. So a column added
#: tomorrow is unhelpful here rather than wrong, and
#: ``test_no_journal_entry_ever_renders_an_identifier`` is what says nobody has quietly started
#: relying on the other behaviour.
NAMED_BY: dict[str, str] = {
	"assignee_id": USER,
	"assigned_by_id": USER,
	"claimed_by_id": USER,
	"owner_id": USER,
	"created_by": USER,
	"updated_by": USER,
	"status_id": STATUS,
	"type_id": TYPE,
	"project_id": PROJECT,
	"parent_task_id": TASK,
	"recurrence_template_id": TASK,
}


def identifier (value: typing.Any) -> uuid.UUID | None:
	"""Return ``value`` as an id if that is what it is, and ``None`` otherwise.

	``event.changes`` is JSON, so what comes back is a string on both backends rather than the
	``uuid.UUID`` the column held. Parsing is the check: a value that is not one raises and is
	simply not an id, which is the same answer as a column nobody declared.
	"""

	if isinstance(value, uuid.UUID):
		return value

	if not isinstance(value, str):
		return None

	try:
		return uuid.UUID(value)

	except ValueError:
		return None


def wanted (
	rows: typing.Sequence[subroutine.db.models.activity.Event],
) -> dict[str, set[uuid.UUID]]:
	"""Return which ids each lookup must fetch to render this page, keyed by lookup.

	**Both sides of every change**, because a journal says what something moved *from* as well
	as what it moved to — and *In progress to Done* is the sentence, where *to Done* leaves a
	reader to remember what it was.

	**Actors are in here too**, under :data:`USER`. They are the one id on an event that is not
	inside ``changes``, and forgetting them is how a page resolves every status perfectly and
	still says a UUID did it.
	"""

	found: dict[str, set[uuid.UUID]] = {
		USER: set(), STATUS: set(), TYPE: set(), PROJECT: set(), TASK: set()
	}

	for row in rows:
		if row.actor_user_id is not None:
			found[USER].add(row.actor_user_id)

		if not isinstance(row.changes, dict):
			continue

		for field, moved in row.changes.items():
			lookup = NAMED_BY.get(field)

			if lookup is None or not isinstance(moved, dict):
				continue

			for side in ("from", "to"):
				# **Not `identifier`**, which is the name of the function two lines up — and
				# assigning to it here shadowed it for the rest of the loop, so the *second*
				# side of the first change raised. `#1409`'s defect: a name bound twice, and
				# Python taking the later binding.
				found_id = identifier(moved.get(side))

				if found_id is not None:
					found[lookup].add(found_id)

	return found


def said (
	session: sqlalchemy.orm.Session,
	rows: typing.Sequence[subroutine.db.models.activity.Event],
) -> dict[uuid.UUID, str]:
	"""Return what each comment on this page actually said, keyed by the comment's own id.

	**The one thing the feed omits and the whole reason this module exists.** A
	``comment.created`` event names the comment as its entity and says nothing about its
	contents, so 29% of a day's feed is a row reporting only that somebody wrote something.

	**A deleted comment is absent rather than empty.** Deletion is soft, so the row and its body
	are both still there — and showing them would make the journal the one surface where a
	retracted paragraph is still readable. Absent is the same answer the mention index gives:
	a deleted comment stops mentioning anything, because a backlink to a sentence nobody can
	read is worse than none.

	One query for the page, whatever its size, which is `#39`'s rule and the reason
	:func:`subroutine.domain.events.descriptions` next door is shaped the same way.
	"""

	model = subroutine.db.models.activity.Comment
	wanted_ids = {
		row.entity_id
		for row in rows
		if row.entity_type == "comment" and row.entity_id is not None
	}

	if not wanted_ids:
		return {}

	found = session.execute(
		sqlalchemy.select(model.id, model.body).where(
			model.id.in_(wanted_ids), model.deleted_at.is_(None)
		)
	).tuples()

	return {identifier: body for identifier, body in found if body}
