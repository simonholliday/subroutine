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
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.scoping
import subroutine.domain.text

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


#: The fields whose value is a whole text, on any kind of thing — `#2728`. **A journal entry
#: never carries one** (Simon, 2026-09-16): a change to any of these says that it changed and
#: nothing either side, and the audit log keeps both sides whole for anybody who needs them.
#:
#: **By field name rather than per entity**, and wider than
#: :data:`subroutine.domain.events.PROSE_FIELD` on purpose. That one names the field whose
#: replacement counts as a *revision*, one per kind; this is every field that holds prose at all,
#: which is also a comment's body when it is edited, a project's description and a workspace's.
#: Measured on the served instance's latest 5,000 events: the text inside their changes was
#: 5.2 MB of documents' bodies and 0.7 MB of tasks' descriptions, against 0.2 MB for every
#: other field together.
#: ``test_every_prose_field_is_a_whole_text`` holds the one inside the other.
#:
#: **A title is one too, for the journal's reason rather than for its size** (Simon,
#: 2026-09-17, `#2853`). The item's row already shows the title it has now, so the two either
#: side of *to* said nothing a reader needed - and they made the longest lines: on this
#: instance's latest 400 entries, all six title changes were over 140 characters, up to 367,
#: where every kind of line but a comment was at most 53.
WHOLE_TEXTS: frozenset[str] = frozenset({"description", "body", "title"})

#: How much of a comment an entry carries — `#2728`. It was 280, measured to keep most first
#: paragraphs whole, and **Simon halved it on 2026-09-17** (`#2852`) when line breaks became
#: spaces: on the latest 48 comments in the journal 23 reached 280, about three lines of the
#: page each, and 140 keeps the first sentence - the heading an agent writes first - in one
#: and a half.
OPENING = 140


class Said(typing.NamedTuple):
	"""The opening of what a comment said, and whether there was more of it."""

	opening: str
	cut: bool


def _not_a_rule_bearing_row () -> typing.Any:
	"""Refuse an entry about the hidden row a repeat keeps its rule on - `#2849`.

	**One act by a person is one entry** (Simon, 2026-09-20). Giving an item a repeat makes a
	second row that holds the rule, in no listing and reachable only by a number nobody was
	shown, and the journal printed its creation above the change that made it: *created #2
	Water the plants*, then *updated #1 Water the plants*. A reader met a twin of the item
	they were looking at, and nothing said what it was. Decision `#1249`'s framing is the
	argument: a repeating item is **one** thing to the person who filed it.

	**Nothing is lost by leaving it out.** The act itself is recorded against the item a
	reader can reach (`#2825`), the series' own history is on its own page for anybody holding
	its number, and the events are untouched - so ``/v1/changes``, the feed a client polls,
	still carries them. That is the split `#1429` made: one store, two readings.

	**The same flag every listing already uses**, ``task.is_template``, rather than a second
	description of what a series row looks like.

	**Asked of each event's own task, by its key** (the cold review of 2026-09-21, `#3159`). This
	was ``entity_id IN (every template in the installation)``, unnarrowed, over a column with no
	index - so each page, and the browser polls pages, read the whole task table to take a
	handful of rows out of a page of fifty. A correlated ``EXISTS`` looks up one task by its
	primary key for each event about a task, and grows with the page rather than the install.
	"""

	event = subroutine.db.models.activity.Event
	task = subroutine.db.models.work.Task

	return ~(
		(event.entity_type == TASK)
		& sqlalchemy.exists().where(task.id == event.entity_id, task.is_template.is_(True))
	)


def page (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	size: int,
	mine: bool = False,
	by: uuid.UUID | None = None,
	oldest: bool = False,
	narrowing: typing.Sequence[typing.Any] = (),
) -> tuple[list[subroutine.db.models.activity.Event], bool]:
	"""Return one page of the journal and whether more lie beyond it — `#2772`.

	**The latest entries, newest first**, or with ``oldest`` the first of a period, oldest first.
	Simon's decision of 2026-09-16, and his reason is the next page: read from the newest, the
	page after this one is the entries before it, and read from the oldest it is the entries
	after. The last hundred oldest first would leave the next page meaning nothing.

	**Here rather than in each transport**, because the rows are :func:`events.page`'s and that
	function always answers forwards, which is right for the feed a cursor resumes. Two routes
	reversing it for themselves is two answers to one question waiting to disagree.

	**And a repeat's rule-bearing row is left out here rather than in the feed** (`#2849`),
	because it is this reading of the events that has a person in front of it.
	:func:`_not_a_rule_bearing_row` carries the argument.
	"""

	rows, more = subroutine.domain.events.page(
		session,
		principal,
		workspace_ids=workspace_ids,
		size=size,
		mine=mine,
		by=by,
		newest=not oldest,
		# A condition beside the caller's filters rather than one of them: both are clauses
		# this statement is narrowed by, and only this one is the journal's own rule.
		narrowing=(*narrowing, _not_a_rule_bearing_row()),
	)

	if not oldest:
		rows.reverse()

	return rows, more


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
	described: typing.Mapping[uuid.UUID, subroutine.domain.events.Described],
) -> dict[str, set[uuid.UUID]]:
	"""Return which ids each lookup must fetch to render this page, keyed by lookup.

	**Both sides of every change**, because a journal says what something moved *from* as well
	as what it moved to — and *In progress to Done* is the sentence, where *to Done* leaves a
	reader to remember what it was.

	**Actors are in here too**, under :data:`USER`. They are the one id on an event that is not
	inside ``changes``, and forgetting them is how a page resolves every status perfectly and
	still says a UUID did it.

	**And what each entry's item is and where it is filed** (`#2727`), out of ``described``,
	which loaded them with the item's ref and title. Under the same lookups as a change's, so
	the project an item is filed in passes :func:`readable_only` like every other project an
	entry names, rather than being the one name on the entry that skipped it.
	"""

	found: dict[str, set[uuid.UUID]] = {
		USER: set(), STATUS: set(), TYPE: set(), PROJECT: set(), TASK: set()
	}

	for about in described.values():
		if about.type_id is not None:
			found[TYPE].add(about.type_id)

		if about.project_id is not None:
			found[PROJECT].add(about.project_id)

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


def readable_only (
	session: sqlalchemy.orm.Session,
	needed: dict[str, set[uuid.UUID]],
	*,
	principal: subroutine.domain.authentication.Principal,
	workspace_ids: typing.Sequence[uuid.UUID],
) -> dict[str, set[uuid.UUID]]:
	"""Keep only the ids inside a page's changes that its reader may see named — `#2726`.

	**An entry can be visible while something named inside it is not.** Whether a reader gets an
	entry at all is decided by the item it is about; what moved *inside* the change is another
	row. A task moved out of a private project into an open one is rightly shown to somebody
	outside the private project, and its entry said *where it is filed: 'secret' to 'open'* to
	them - measured on both backends before this existed.

	**A project and a task are the lookups that can name something private.** An account, a
	status and a type belong to the workspace, and everybody reading it may know them. What is
	dropped here renders as nothing, exactly as an id nobody can name already does.
	"""

	kept = dict(needed)

	for lookup in (PROJECT, TASK):
		if not needed[lookup]:
			continue

		statement = subroutine.domain.scoping.readable_among(
			principal, workspace_ids=workspace_ids, kind=lookup, identifiers=needed[lookup]
		)

		# A kind this credential cannot read at all names nothing of that kind.
		kept[lookup] = set() if statement is None else set(session.scalars(statement))

	return kept


def said (
	session: sqlalchemy.orm.Session,
	rows: typing.Sequence[subroutine.db.models.activity.Event],
) -> dict[uuid.UUID, Said]:
	"""Return how each comment on this page opens, keyed by the comment's own id.

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

	**Only the opening, and cut here rather than by whoever renders it** (`#2728`), so no whole
	comment leaves this function: :data:`OPENING` characters on one line, ended at a word unless
	that would keep fewer than a third of them, and marked.
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

	return {
		identifier: Said(*subroutine.domain.text.opening(body, OPENING))
		for identifier, body in found
		if body
	}
