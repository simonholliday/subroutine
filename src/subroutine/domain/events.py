"""Recording what happened, in the same transaction as the thing that happened.

docs/design.md §10.7 invariant 9: every entity mutation emits at least one ``event`` row, written
inside the caller's transaction. That "inside" is the whole point — an event dispatched
afterwards can be lost when the mutation is rolled back, or recorded for a change that
never landed, and either way the audit trail becomes something you have to corroborate
rather than something you can read.

One table serves four purposes: the audit trail, the activity feed, the change feed
clients poll for what happened while they were away, and the outbox a webhook dispatcher
will later drain. That is why the cost is paid on every write from the first migration
rather than added when someone wants a feed.
"""

import dataclasses
import datetime
import enum
import hashlib
import typing
import uuid

import sqlalchemy
import sqlalchemy.dialects.postgresql
import sqlalchemy.dialects.sqlite
import sqlalchemy.orm
import sqlalchemy.orm.attributes
import sqlalchemy.sql.util
import sqlalchemy.sql.visitors

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.scoping
import subroutine.domain.sounds
import subroutine.errors


class EventAction(enum.StrEnum):
	"""What was done to an entity.

	Stored as text rather than a database enum, and open by design: a later feature adds
	its verbs here without a migration. The values are read by clients, so they are as
	stable as the error codes.

	**Every member is one something records** (`#2723`), which ``tests/test_event_actions.py``
	holds. ``status_changed`` and ``completed`` were declared here as reserved for slice 2 and
	were never written - a finish has always been an ``updated`` with the status and
	``completed_at`` in its ``changes`` - and Simon's call of 2026-09-17 was to delete them
	rather than start writing them, since that would change what every reader of ``updated``
	is sent. A verb arrives with the code that records it.
	"""

	CREATED = "created"
	UPDATED = "updated"
	DELETED = "deleted"
	RESTORED = "restored"
	MOVED = "moved"

	#: A workspace was stocked with its vocabulary, or an upgrade added to it. Carries the
	#: seed version and the per-kind counts rather than one event per row.
	SEEDED = "seeded"

	#: A lease was taken on a task, renewed, or given back (§14.11, `#350`). Recorded because
	#: "who was working on this and gave up" is otherwise unanswerable — a claim that expires
	#: leaves no trace in the row, which is the whole point of a lease and would make the
	#: history the only place the attempt existed.
	CLAIMED = "claimed"
	RELEASED = "released"


def record (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	entity_id: uuid.UUID,
	action: str,
	subject_type: str | None = None,
	subject_id: uuid.UUID | None = None,
	subject_b_type: str | None = None,
	subject_b_id: uuid.UUID | None = None,
	changes: dict[str, typing.Any] | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.activity.Event:
	"""Append one event to the change feed.

	``actor`` is optional because some writes have no principal behind them: seeding, a
	migration's data fix, and ``subroutine init`` all happen before anyone has logged in.
	Recording those as system actions is more honest than attributing them to whoever
	happened to run the command.

	``subject_*`` names what the event happened *on* when that is something other than the
	entity itself. Comments and links pass it — a comment's subject is the item it was written
	on, a link's is the item it hangs off (`#252`) — and it is what makes both visible exactly
	when that item is. See ``selected`` for what reads it, and ``scoping.visible_events`` for
	why a kind without either a clause or a subject reaches nobody.

	``subject_b_*`` names a **second** one, for a write that happened on two items (`#302`).
	Only links pass it today. It narrows and never widens: an event carrying one is visible
	only to somebody who may see *both*, which is the conjunction a single subject cannot
	express. Setting it on something that happened on one item would hide the event from
	nobody, but it would be a false statement about what the write touched.
	"""

	event = subroutine.db.models.activity.Event(
		workspace_id=workspace_id,
		actor_user_id=None if actor is None else actor.user.id,
		actor_token_id=None if actor is None or actor.token is None else actor.token.id,
		# **One line reaches every event write, including ones nobody has written yet** —
		# `#1415`, and `#405`'s rule about putting a check where everything must pass, applied
		# to a field. This is the only place an `Event` is constructed, and it already reads
		# the actor's other two identity facts off the principal.
		actor_interface=None if actor is None else actor.interface,
		entity_type=entity_type,
		entity_id=entity_id,
		subject_type=subject_type,
		subject_id=subject_id,
		subject_b_type=subject_b_type,
		subject_b_id=subject_b_id,
		action=action,
		# **A long text is kept once and the event carries its hash** (`#578`); :func:`whole` puts
		# it back for every reader.
		changes=None if changes is None else _stored(session, workspace_id, jsonable(changes)),
	)
	session.add(event)
	# **Every event, whatever wrote it**, for a workspace that sends what happens in it over OSC
	# (`#2722`) - composed as the transaction commits, and dropped if it rolls back.
	session.info.setdefault(subroutine.domain.sounds.PENDING, []).append(event)

	return event


#: Which changed fields mean an item's **content** changed rather than its bookkeeping.
#:
#: **The question this answers, and it is one question**: did the substance of this item change
#: — what it is and what it asks of you — as opposed to where it sits, when it is planned, who
#: is holding it, and how it is ranked? That is what ``content_updated_at`` reports and what a
#: reader deciding whether to re-read an item wants. `#1112` is the item, and decision `#1141`
#: carries the argument for every line of both sets.
#:
#: **Two other questions used to be asked of the same column and neither is asked here.** The
#: evidence gate binds a verification to the *tree it ran against* rather than to a timestamp on
#: the ticket, because a task's row does not move when the code does. And interrupt
#: classification needs facts that are not on this row at all — a decision superseded elsewhere,
#: a dependency regressing — so it reads the event, of which this is one part.
#:
#: **Every field the comparison can produce appears in exactly one of the two sets**, which is
#: what makes adding a column a decision rather than a default: ``tests/test_content_changes.py``
#: fails on a field in neither, and on an entry naming a field that no longer exists. A deadline
#: was lost that way — specified as content in two places, absent from the code, and invisible
#: because the guard that existed sampled one field from each side of the line.
CONTENT_FIELDS: dict[str, frozenset[str]] = {
	"task": frozenset(
		{
			"title",
			"description",
			# **The type decides what mood the title is in** — a `bug` retyped as a
			# `question` has had the sentence its title makes change under a reader. Absent from
			# §6.1's list, which is an omission there rather than a mistake here.
			"type_id",
			"status_id",
			# **A deadline is a commitment; a planned day is an intention.** That is the line
			# §6.1 draws by naming `due_at` as content and `plan`/`defer` as bookkeeping, and
			# it is the half the code had lost. The flag is content for the same reason: *by
			# Friday* and *at Friday 00:00* are different promises about the same instant.
			"due_at",
			"due_is_all_day",
			# **Content rather than bookkeeping, and it is a judgement** (`#1268`). Every other
			# scheduling field says *when* one piece of work happens, which §6.1 calls
			# bookkeeping. This says the work happens **again** — *water the plants* and *water
			# the plants every fortnight* are different undertakings, not the same one
			# rescheduled, and a reader who saw the first is looking at something else now.
			"recurrence_template_id",
			# The same judgement about the rule itself (`#2825`): every Monday and every Tuesday
			# are two undertakings, and changing one into the other used to record nothing.
			"recurrence",
		}
	),
	"document": frozenset(
		{
			"title",
			"body",
			# **A document's status is what decides whether it binds.** `subroutine://conventions`
			# is `type=decision&status=active`, so a decision moving to `superseded` stops being
			# in force — which is a larger change to what it means than most edits to its body.
			# Neither this nor the type counted until `#1112`; both do on a task, and one rule
			# reading two ways for two entities is what that item was filed about.
			"status_id",
			"type_id",
			# **Whom it is in force for** (`#4133`): a decision marked to bind the whole workspace
			# asks something of every reader of it, which is the status's argument one step wider.
			"binds",
		}
	),
}

#: Which changed fields are **bookkeeping** — real changes that do not alter what an item means.
#:
#: Declared rather than inferred as *whatever is left*, so that adding a column and forgetting
#: this file fails the build instead of quietly defaulting to bookkeeping. Every entry needs a
#: reason, and the reason is what makes it re-askable.
BOOKKEEPING_FIELDS: dict[str, frozenset[str]] = {
	"task": frozenset(
		{
			# Who, not what. Handing work over does not change the work.
			"assignee_id",
			# Ranking. §6.3's two axes say where this sits in a queue.
			"importance",
			"urgency",
			# How much, not what. An estimate is a claim about effort.
			"estimate_minutes",
			# §6.1 names `plan` and `defer` as bookkeeping by name. A start date and a snooze
			# are when somebody intends to get to it, which is theirs to change freely.
			"starts_at",
			"starts_is_all_day",
			# **Beside its start rather than beside `due_at`, and that is a judgement** — a
			# span is *when* something happens, which §6.1 puts here, where a deadline is a
			# promise about finishing. Moving a booked holiday is rearranging your own
			# diary; moving a deadline is changing what was undertaken.
			#
			# **Settled by Simon on 2026-09-27, for work as well as events** (`#1314`): when
			# something happens is not what it is, so a verification does not go stale because
			# its slot moved, and rescheduling does not reset how long an item has sat untouched.
			# Weighed and refused: content for work alone, which would make a field mean one
			# thing or the other by the item's type; and content always, which would stale every
			# verification on a holiday somebody moved.
			"ends_at",
			"snoozed_until",
			"snoozed_is_all_day",
			# §6.1 names repositioning. **This is why tags are here too**: a project is a
			# stronger classification than a tag, so a rule counting the weaker one and not the
			# stronger one would be incoherent. Tags counted until `#1112` and neither §6.1 nor
			# §15.4 ever listed them.
			"project_id",
			"tags",
			# The zone the dates were authored in (`#1014`). It re-renders a deadline without
			# moving the instant it names, and the instant is the promise.
			"timezone",
			# When somebody wants to be nudged, which is not what the work is. It rides on the
			# calendar feed (`#1211`) and moving it changes nothing anybody undertook.
			"reminder_minutes",
			# Derived from the status beside it and never moves alone (§10.7 invariant 5), so
			# this entry decides nothing — it is here because the comparison can produce it and
			# every field it can produce is classified.
			"completed_at",
		}
	),
	"document": frozenset(
		{
			# Who maintains it. The document is unchanged.
			"owner_id",
			"project_id",
			"tags",
		}
	),
}


def touches_content (entity_type: str, changes: typing.Mapping[str, typing.Any]) -> bool:
	"""Say whether a set of changes altered what an item means, rather than its bookkeeping.

	Takes what actually **changed** rather than what was sent. Those are different questions
	and answering the second was `#1140`: a client that reads an item, edits one field and
	sends the whole object back names its title in every request, so asking "was a title
	given" recorded a change of meaning on every bookkeeping write such a client made.

	An entity with no content fields declared answers ``False`` rather than raising. Nothing
	yet asks this of a project or a comment, and a classifier that refused by name would have
	to be edited before an unrelated caller could ask an honest question.
	"""

	return bool(CONTENT_FIELDS.get(entity_type, frozenset()) & changes.keys())


#: The one field whose replacement is a **revision** — the prose a reader reads for the
#: conclusion, as opposed to everything else an update can touch.
#:
#: **Narrower than :data:`CONTENT_FIELDS` on purpose** (`#1768`). A title changing is a rename
#: and a reader sees it; a status changing is reported by the status. What nothing said was
#: that the *body* had been replaced — so a fifth draft read exactly like a first, which is
#: decision `#1766`'s cause rather than its symptom: editing was the invisible channel and
#: commenting the visible one, so anybody wanting their reasoning seen picked the comment.
#:
#: **One field per entity rather than a set**, because there is one, and a set would invite a
#: second answer to *has this been revised* the day somebody added to it.
#: ``test_every_prose_field_is_content`` holds each of these inside :data:`CONTENT_FIELDS`,
#: so a field that stopped counting as content could not go on counting as a revision.
PROSE_FIELD: dict[str, str] = {"task": "description", "document": "body"}


@dataclasses.dataclass(frozen=True)
class Revisions:
	"""How many times an item's prose has been replaced, and who last replaced it."""

	#: How many times it was rewritten. Never zero — a first draft has no revisions and is
	#: reported as ``None`` rather than as a count of nothing, which is §12.2a's rule.
	count: int

	#: When the last rewrite happened.
	last_at: datetime.datetime

	#: Who did it, by username, or ``None`` where the event records no actor — a migration
	#: or a caller with no credential (§12.1a). *Somebody* revised it either way, so the
	#: count still stands and only the name is missing.
	last_by: str | None


def _replaced_something (change: typing.Any) -> bool:
	"""Say whether a recorded change **replaced** prose, rather than writing it for the first time.

	**Found by driving it** (`#1768`): a task captured from one line has no description, so
	the first ``update --description`` records ``{"from": null, "to": "A plan."}`` — and
	counting that said *revised twice* about something written once and corrected once. A
	document did not show it, because ``doc create --body`` writes the body at creation and
	its first update really is a replacement.

	An empty string counts as nothing having been there, for the same reason ``None`` does:
	both are an item with no prose, and which one is stored depends on how it was made.
	"""

	return isinstance(change, dict) and bool(change.get("from"))


def revisions_of (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	entity_id: uuid.UUID,
) -> Revisions | None:
	"""Say how often an item's prose has been rewritten, or ``None`` if it never has.

	**Derived from the events rather than counted onto the row** (`#1768`). A column would be
	a second answer to a question the event feed already answers, and this project's own
	record of what that costs is long enough. The old text is in ``event.changes`` whole, so
	nothing here is reconstructing anything — it is reading what is already stored.

	**Indexed**: ``ix_event_workspace_id_entity_type_entity_id_seq`` is exactly this lookup,
	added for the history endpoint, so the rows come back without a scan.

	**Filtered in Python rather than in SQL, and that is a portability decision.** ``changes``
	is a JSON column and the two backends disagree about how to ask what a key contains —
	§10.3's first review dimension, and the kind of difference that works on SQLite and fails
	in CI. The rows are an item's own updates, which is tens rather than thousands, so the
	cost of reading them is what it would have been anyway.

	**Only the prose counts.** :data:`PROSE_FIELD` says which field that is; a rename or a
	status move is a change somebody can already see.

	**And only a replacement counts**, which :func:`_replaced_something` decides: writing a
	description onto a task that never had one is the first draft, not a revision of it.
	"""

	field = PROSE_FIELD.get(entity_type)

	if field is None:
		return None

	# **Both tables** (`#251`): a rewrite moved to the archive past a retention floor is still one.
	model = HISTORY
	actor = sqlalchemy.orm.aliased(subroutine.db.models.identity.User)

	rows = session.execute(
		sqlalchemy.select(model.created_at, model.changes, actor.username)
		.outerjoin(actor, actor.id == model.actor_user_id)
		.where(
			model.workspace_id == workspace_id,
			model.entity_type == entity_type,
			model.entity_id == entity_id,
			model.action == EventAction.UPDATED,
		)
		.order_by(model.seq)
	).all()

	rewrites = [
		(created_at, username)
		for created_at, changes, username in rows
		if changes is not None and _replaced_something(changes.get(field))
	]

	if not rewrites:
		return None

	last_at, last_by = rewrites[-1]

	return Revisions(count=len(rewrites), last_at=last_at, last_by=last_by)


def changes_between (
	before: dict[str, typing.Any], after: dict[str, typing.Any]
) -> dict[str, typing.Any]:
	"""Return the fields that differ, as ``{"field": {"from": …, "to": …}}``.

	Only what actually changed: an update that sets a field to the value it already held
	should not appear in the feed as though something happened, or every client polling
	for real changes has to filter them back out.
	"""

	differences: dict[str, typing.Any] = {}

	for field, new_value in after.items():
		old_value = before.get(field)

		if old_value != new_value:
			differences[field] = {"from": jsonable(old_value), "to": jsonable(new_value)}

	return differences


#: The fields whose long values are kept once rather than in every event - `#578`. The two that
#: hold prose, and two the journal shows as a phrase without its values, so no reader of a value
#: meets a reference. A title never reaches :data:`STORED_FROM`.
STORED_FIELDS = frozenset({"description", "body"})

#: How long a value is before it is kept once rather than carried. Short ones cost less inline
#: than a hash and a row would.
STORED_FROM = 256

#: The one key of what an event carries in place of a text it keeps elsewhere.
REFERENCE = "sha256"


def _stored (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, changes: typing.Any
) -> typing.Any:
	"""Return ``changes`` with each long text replaced by a reference to its one stored copy."""

	if not isinstance(changes, dict):
		return changes

	kept = dict(changes)

	for field in sorted(STORED_FIELDS & kept.keys()):
		sides = kept[field]

		if not isinstance(sides, dict):
			continue

		replaced = dict(sides)

		for side in ("from", "to"):
			text = replaced.get(side)

			if isinstance(text, str) and len(text) >= STORED_FROM:
				replaced[side] = {REFERENCE: _keep(session, workspace_id, text)}

		kept[field] = replaced

	return kept


def _keep (session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, text: str) -> str:
	"""Store ``text`` for this workspace unless it is there already, and return its hash.

	**An insert that does nothing when the text is already kept**, rather than a read first: two
	writers keeping one text at once would otherwise race, and the second would fail a whole
	request over a copy the first had just made.
	"""

	digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
	table = subroutine.db.models.activity.TEXTS
	dialect = session.get_bind().dialect.name
	insert = (
		sqlalchemy.dialects.postgresql.insert(table)
		if dialect == "postgresql"
		else sqlalchemy.dialects.sqlite.insert(table)
	)
	session.execute(
		insert.values(workspace_id=workspace_id, sha256=digest, text=text).on_conflict_do_nothing()
	)

	return digest


def _references (changes: typing.Any) -> typing.Iterator[str]:
	"""Yield the hash of every text ``changes`` keeps elsewhere."""

	if not isinstance(changes, dict):
		return

	for field in STORED_FIELDS & changes.keys():
		sides = changes[field]

		if not isinstance(sides, dict):
			continue

		for side in ("from", "to"):
			held = sides.get(side)

			if isinstance(held, dict) and isinstance(held.get(REFERENCE), str):
				yield held[REFERENCE]


def restored (
	changes: typing.Any, found: typing.Callable[[str], str | None]
) -> typing.Any:
	"""Return ``changes`` with each reference replaced by the text ``found`` gives for its hash."""

	if not any(_references(changes)):
		return changes

	kept = dict(changes)

	for field in STORED_FIELDS & kept.keys():
		sides = kept[field]

		if not isinstance(sides, dict):
			continue

		replaced = dict(sides)

		for side in ("from", "to"):
			held = replaced.get(side)

			if isinstance(held, dict) and isinstance(held.get(REFERENCE), str):
				replaced[side] = found(held[REFERENCE])

		kept[field] = replaced

	return kept


def refuse_a_kept_reference (changes: typing.Any) -> None:
	"""Refuse to render ``changes`` while it still names a text kept elsewhere - `#4297`.

	**A backstop where a value reaches a reader**, rather than on load, which would cost a query
	per row. Every renderer today asks :func:`descriptions` first, which puts the texts back; one
	that did not would publish ``{"sha256": ...}`` where the text belongs, and nothing would say so.
	"""

	if any(_references(changes)):
		raise subroutine.errors.InternalError(
			"An event was about to be shown with a reference where its text belongs.",
			hint="Read the rows through events.descriptions() or events.whole() before rendering.",
		)


def whole (
	session: sqlalchemy.orm.Session, rows: typing.Sequence[subroutine.db.models.activity.Event]
) -> None:
	"""Put each kept text back into ``rows``, as it was written - `#578`.

	**One read for the page**, of every hash its rows name. **Set as the value loaded, never as a
	change**, so the session has nothing to write back and the stored row keeps its references.
	"""

	wanted = {digest for row in rows for digest in _references(row.changes)}

	if not wanted:
		return

	table = subroutine.db.models.activity.TEXTS

	# **By workspace as well as hash** (`#4294`, M14 of the cold review of 2026-10-03), so the
	# ``(workspace_id, sha256)`` key answers it: by hash alone it scanned the whole table, on every
	# feed page naming a kept text - which a poll re-reading its last event can do every few seconds.
	texts = {
		(found.workspace_id, found.sha256): found.text
		for found in session.execute(
			sqlalchemy.select(table).where(
				table.c.workspace_id.in_({row.workspace_id for row in rows}),
				table.c.sha256.in_(wanted),
			)
		)
	}

	for row in rows:
		named = {digest: texts.get((row.workspace_id, digest)) for digest in _references(row.changes)}

		if named:
			sqlalchemy.orm.attributes.set_committed_value(
				row, "changes", restored(row.changes, named.get)
			)


#: Every event this instance holds, the live table's and the archive's (`#251`, decision `#4233`).
#:
#: **The change feed reads the live table, and everything that reads history reads this**: the
#: journal, an item's history, *revised N times* and an export, and the ``touched_at`` and
#: ``touched_by`` filters through two clauses of their own. An operator's retention floor moves
#: old events out of the feed, so its cursors expire as §5.11 says, and takes nothing from the
#: record.
EVERY_EVENT = sqlalchemy.union_all(
	sqlalchemy.select(subroutine.db.models.activity.Event.__table__),
	sqlalchemy.select(subroutine.db.models.activity.ARCHIVE),
).subquery("every_event")

#: :data:`EVERY_EVENT` read as events, so a reader of history is handed the rows the feed is.
HISTORY = sqlalchemy.orm.aliased(subroutine.db.models.activity.Event, EVERY_EVENT, name="history")


def held (clause: typing.Any) -> typing.Any:
	"""Return ``clause``, written about the live table, as asked of every event held - `#251`.

	**One translation rather than a second copy of each predicate.** Who may see an event, the
	filters and the journal's own rule are written once, against :class:`Event`, and this re-points
	their columns at :data:`EVERY_EVENT` - a correlated ``EXISTS`` inside one included.
	"""

	return sqlalchemy.sql.util.ClauseAdapter(EVERY_EVENT).traverse(clause)


def selected (
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	entity_type: str | None = None,
	entity_id: uuid.UUID | None = None,
	upper_bound: datetime.datetime | None = None,
	since: int | None = None,
	before: int | None = None,
	visible: sqlalchemy.ColumnElement[bool] | None = None,
	actor_token_id: uuid.UUID | None = None,
	narrowing: typing.Sequence[typing.Any] = (),
	everything: bool = False,
) -> sqlalchemy.Select[subroutine.db.models.activity.Event]:
	"""Return the statement both readers of this table are built on (docs/design.md §5.11a).

	**One builder, and the upper bound is a parameter** — that is the whole design, and it is
	the thing that stops a per-entity history being written as "the feed with a filter".
	``GET /v1/changes`` will pass a watermark of ``now() - 1s``, because it is *resumable*:
	``seq`` is allocated at insert and becomes visible at commit, so a reader that advances
	its cursor past an uncommitted number never sees that event again. A **history** passes
	nothing, because it is not resumable — ask again and the row is still there — and a
	history that inherited the watermark would show nothing for a comment written a moment
	ago, which a person meets in the first minute and reads as a lost write.

	The ordering is the caller's, deliberately: a history runs newest-first and the feed runs
	forwards, and both are served by an index the schema already carries
	(``ix_event_workspace_id_entity_type_entity_id_seq`` and ``ix_event_workspace_id_seq``).

	**This narrows by workspace and nothing else**, and who may see an event is ``visible``,
	which :func:`feed` and :func:`history` both pass. **A history used to pass nothing**, on
	the argument that resolving its item through ``readable_tasks``/``_projects``/``_documents``
	was the permission check. That held for a comment, which is exactly as visible as the item
	it is on, and stopped holding for a link when a link became visible only where both of its
	ends are (`#302`): the item's history handed its reader a link to one they could not see,
	with that item's ref in ``changes`` (`#2769`).

	**The last three arguments belong to the feed alone**, and are stated here so that both
	readers are still built by one function rather than two that agree for a while:

	* ``since`` is a ``seq`` and is **inclusive**. §5.11 fixes cursors as
	  "inclusive-with-dedupe" because a client that persists its cursor before it has finished
	  processing a page must not lose the page — one duplicated row per poll buys that, and
	  every event carries a stable ``id`` to dedupe on.
	* ``before`` is a ``seq`` and is **exclusive**, which is the opposite and is deliberate
	  (`#1097`). It is not a cursor a client persists between polls: it is how the page just
	  read is resumed *backwards*, inside one call, from a row already in hand — so re-asking
	  for that row would return a duplicate rather than protect against a lost one. The two
	  bounds compose, and together they are a range.
	* ``visible`` is :func:`subroutine.domain.scoping.visible_events`. It is a *parameter* so
	  that this stays a builder rather than a policy — :func:`feed` and :func:`history` are the
	  two places that decide, and both always narrow.
	* ``actor_token_id`` answers "what did *I* do" (`#158`) — **the credential, not the user**.
	  An agent with its own service-account token wants what it did, not what the person who
	  issued it did from a laptop.
	* ``narrowing`` is §9.6's dotted filters already compiled — `#1431`, decision `#1429`.
	  Compiled by :mod:`subroutine.domain.filtering` rather than here, because the grammar,
	  the refusals and what `/v1/meta` publishes are one thing and a feed must not grow a
	  second copy of them. **It arrives already read**, so this stays a builder and never
	  needs a clock or a timezone of its own.

	  **``since`` and a date range answer different questions and both are kept.** A cursor
	  resumes and is inclusive-with-dedupe; a range is a statement about a period and is not
	  resumable. A caller asking *what did we do on Friday* has no cursor to offer, and one
	  polling has no date in mind.

	**Naming an entity asks for what happened *to* it, which is not the same as what was
	recorded *against* it.** Commenting on ``#42`` writes an event whose entity is the comment,
	so a query matching only ``entity_id`` reported that nothing had happened to an item
	somebody had just written a paragraph about — both ways of asking blind at once, since
	``updated_at`` deliberately does not move for a comment either (``#52``). The subject pair
	is the join that fixes it, and matching it here rather than at the route means the feed
	inherits the same answer instead of inventing a second one.

	**``everything`` reads the archive as well as the live table** (`#251`), and is for a reader of
	history: an item's history, the journal and an export. The change feed never asks for it, since
	its cursor is the thing a retention floor expires. Every condition is written about the live
	table once and, for history, asked of both through :func:`held`.
	"""

	model = subroutine.db.models.activity.Event
	conditions: list[typing.Any] = [model.workspace_id.in_(workspace_ids)]

	if entity_type is not None and entity_id is not None:
		conditions.append(
			sqlalchemy.or_(
				sqlalchemy.and_(model.entity_type == entity_type, model.entity_id == entity_id),
				sqlalchemy.and_(model.subject_type == entity_type, model.subject_id == entity_id),
			)
		)

	else:
		if entity_type is not None:
			conditions.append(model.entity_type == entity_type)

		if entity_id is not None:
			conditions.append(model.entity_id == entity_id)

	if upper_bound is not None:
		conditions.append(model.created_at <= upper_bound)

	if since is not None:
		conditions.append(model.seq >= since)

	if before is not None:
		conditions.append(model.seq < before)

	if visible is not None:
		conditions.append(visible)

	if actor_token_id is not None:
		conditions.append(model.actor_token_id == actor_token_id)

	# Unconditional: an empty sequence narrows by nothing, so every caller passes whatever it
	# was asked without testing first — the same shape `api.filters.narrowed` already has.
	conditions.extend(narrowing)

	if not everything:
		return sqlalchemy.select(model).where(*conditions)

	return sqlalchemy.select(HISTORY).where(*[held(condition) for condition in conditions])


#: How far behind the clock the newest reportable event sits. §5.11 fixes the value because it
#: is client-visible: a caller polling more often than this sees nothing new, and one reasoning
#: about freshness needs to know the feed is deliberately a second stale.
#:
#: **In the domain rather than in the route**, because two clients answer this question — the
#: HTTP endpoint and ``clients.local`` — and a watermark that existed in only one of them would
#: mean the same instance losing events over one transport and not the other. That divergence
#: is what S3-07 removed for tasks and what ``views.py`` sits outside ``api/`` to prevent.
WATERMARK = datetime.timedelta(seconds=1)

#: The lowest ``seq`` a cursor can name. Written down rather than spelled ``1`` at the two
#: places that need it, because a caller sending ``0`` is told this number and would otherwise
#: be told it by a literal nobody had connected to the column it describes.
FIRST_SEQ = 1

#: The highest ``seq`` the column can hold - it is a 64-bit integer on both backends - and so
#: the highest a cursor or a bound can name (`#3933`). Past it the value could not even be
#: bound: both backends answered 500 for ``since=2**63``.
LAST_SEQ = 2**63 - 1


def feed (
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	since: int | None = None,
	before: int | None = None,
	mine: bool = False,
	by: uuid.UUID | None = None,
	newest: bool = False,
	narrowing: typing.Sequence[typing.Any] = (),
	everything: bool = False,
) -> sqlalchemy.Select[subroutine.db.models.activity.Event]:
	"""Return the change feed's statement — ordered, watermarked and narrowed (§5.11a).

	Everything :func:`selected` leaves to the caller, this decides, because for a feed the
	answers are not a caller's to choose: it always runs forwards, it always withholds the last
	second, and it is always narrowed to what the principal may see. A history is the opposite
	on all three counts, and keeping them one function is how they would come to agree only for
	a while.

	``mine`` is ``#158``'s ``?actor=me``. **A caller with no token gets nothing**, rather than
	everything: a session-authenticated principal has no ``actor_token_id`` on anything it
	wrote, so matching on a null token would quietly widen the filter to every system-written
	row — and the belief being tested is precisely "these are the things I did".

	``by`` is the same question at the other grain (`#1120`): *what did that account do*,
	through whatever credential. **It is not a second question, it is a coarser one** — and the
	coarse grain is the only one useful about somebody else, because nobody knows another
	credential's id. On this instance the person writes 2.2% of the events, so *what has it
	been doing* is the commonest thing a human asks about the record and had no query at all.
	"""

	# The archive too for a reader of history, which :func:`selected` explains (`#251`).
	model: typing.Any = HISTORY if everything else subroutine.db.models.activity.Event
	token_id = None if principal.token is None else principal.token.id

	statement = selected(
		workspace_ids=workspace_ids,
		upper_bound=subroutine.db.types.utcnow() - WATERMARK,
		since=since,
		before=before,
		visible=subroutine.domain.scoping.visible_events(
			principal, workspace_ids=workspace_ids
		),
		actor_token_id=token_id if mine else None,
		narrowing=narrowing,
		everything=everything,
	)

	if mine and token_id is None:
		statement = statement.where(sqlalchemy.false())

	if by is not None:
		statement = statement.where(model.actor_user_id == by)

	# **`newest` reads the tail, and :func:`page` turns it the right way up again.** A feed is
	# defined forwards and stays that way in every answer; this is only about which end of a
	# long history the *first* call lands on. Without it somebody meeting an instance with
	# thousands of events is shown its first afternoon and has to page to reach this morning,
	# which is not what "what has changed" asks.
	#
	# **And since `#1097` a `newest` feed is walkable backwards, which that sentence used to
	# rule out.** Simon's decision of 2026-08-28. It had to be: with `newest` set `has_more`
	# means there are *earlier* events, and `since` is a floor, so a client holding the newest
	# page had no way to ask for the rest — a caller asking for 500 got one page over HTTP and
	# 500 locally, from the same command. `before` is that way, and every answer still reads
	# forwards; what is no longer true is that the feed can only be *asked* forwards.
	return statement.order_by(model.seq.desc() if newest else model.seq.asc())


def history (
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	entity_id: uuid.UUID,
) -> sqlalchemy.Select[subroutine.db.models.activity.Event]:
	"""Return what happened to one item, as this principal may be told it — `#2769`.

	**Narrowed exactly as the feed is**, through :func:`subroutine.domain.scoping.visible_events`,
	so an item's history and a workspace's feed cannot disagree about an event. The caller has
	still resolved the item first, which is what makes one the reader may not see absent rather
	than forbidden; this is what then decides which of the events *on* it they may see, and a
	link to something private is the one that resolving the item could not.

	**One function both transports call**, as :func:`feed` is: the route and the local client
	each built this statement themselves, and both left the predicate off.

	**No upper bound.** This is the watermark the feed passes and a history must not: a history
	is not resumable, so a comment written a moment ago has to be in it.
	"""

	return selected(
		workspace_ids=[workspace_id],
		entity_type=entity_type,
		entity_id=entity_id,
		upper_bound=None,
		visible=subroutine.domain.scoping.visible_events(principal, workspace_ids=[workspace_id]),
		everything=True,
	)


def page (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	size: int,
	since: int | None = None,
	before: int | None = None,
	mine: bool = False,
	by: uuid.UUID | None = None,
	newest: bool = False,
	narrowing: typing.Sequence[typing.Any] = (),
	everything: bool = False,
) -> tuple[list[subroutine.db.models.activity.Event], bool]:
	"""Return one page of the feed, **always oldest first**, and whether more follow.

	The one place the ``newest`` reversal is undone, so that no caller can return a feed
	running backwards and no two callers can disagree about how ``has_more`` was counted.

	``has_more`` means "there is another page in the direction you are reading" — later events
	on an ordinary call, *earlier* ones when ``newest`` is set. Either way the page itself
	reads forwards, and its last row is the number to resume from.

	**Which is what ``before`` is for** (`#1097`). Reading backwards, the number to resume from
	is the *first* row rather than the last, and it is exclusive rather than inclusive — the
	page is already in hand, so there is nothing to protect against losing. ``since`` still
	overrules ``newest`` below; ``before`` does not, because it is the bound a backwards walk
	moves rather than a statement about where the caller has got to.

	**And the one place ``since`` overrules ``newest``** (`#310`). Both transports worked that
	rule out for themselves — ``newest=newest and since is None``, written twice — which is the
	shape this module exists to prevent: the watermark and the ``410`` were moved here so that
	a feed could not behave differently over HTTP and locally, and a third rule was left behind
	in both routes. The equivalence suite exercised each argument alone and never together, so
	a divergence in exactly the combination the rule is about would have passed.
	"""

	# `since` says where the caller has got to, which `newest` cannot improve on and would
	# contradict by skipping everything in between.
	newest = newest and since is None

	statement = feed(
		principal,
		workspace_ids=workspace_ids,
		since=since,
		before=before,
		mine=mine,
		by=by,
		newest=newest,
		narrowing=narrowing,
		everything=everything,
	)
	rows = list(session.scalars(statement.limit(size + 1)))
	has_more = len(rows) > size
	rows = rows[:size]

	if newest:
		rows.reverse()

	return rows, has_more


def refuse_a_bound_that_names_nothing (before: int | None) -> None:
	"""Refuse an upper bound below the first ``seq`` there could ever be — `#1097`.

	**The same reasoning as ``since``'s first refusal and not the second.** ``before`` is
	exclusive, so ``before=1`` asks for everything earlier than the first event there has ever
	been: an empty feed, correctly, and one that reads exactly like *nothing has happened*.
	Zero — the ordinary uninitialised default in most languages — is the same answer arrived at
	by accident, and a feed's one unforgivable failure is looking empty when it is not.

	**There is no expiry half.** Nothing is being resumed from here, so an old bound is not a
	lost page; it is a caller asking about the past, which is what a bound is for.

	Here rather than in either caller for §5.11a's reason: a feed must not answer differently
	over two transports, and ``since=0`` is the recorded case of exactly that (`#309`).
	"""

	if before is not None and before > LAST_SEQ:
		raise _past_the_last_seq("before", before)

	if before is None or before > FIRST_SEQ:
		return

	raise subroutine.errors.ValidationError(
		f"'before' is a seq and the first one is {FIRST_SEQ}, so {before} names nothing.",
		code="invalid_field_value",
		errors=[
			subroutine.errors.FieldError(
				field="before",
				code="invalid_field_value",
				message="Send the seq of the earliest event you already have, or omit "
				"'before' to read from the newest end.",
			)
		],
	)


def refuse_unusable_cursor (*, since: int | None) -> None:
	"""Refuse a cursor that names nothing: a ``seq`` past the last any column holds, or below the first.

	**Whether one has expired is :func:`refuse_an_expired_cursor`'s**, asked after the page rather
	than here before it (`#4293`): a run moving events between the two let rows go unreported.

	**Two refusals: a ``seq`` past the last any column holds, and one below the first**
	(`#309`, `#3933`). ``since`` is a ``seq`` and the first one is 1, so ``since=0`` names
	nothing — and, read as a cursor, it is below every
	surviving event and therefore looks exactly like one that expired. That is what happened:
	the route checked ``since >= 1`` for itself and ``clients.local`` did not, so an
	uninitialised cursor — zero, the ordinary default in most languages — got a ``422`` over
	HTTP and, locally, a ``410`` announcing that events had been pruned on an instance that has
	never pruned anything. A refusal that states a cause it has not established is worse than a
	vague one, and this one sent the reader looking for a retention policy that does not exist.

	Both live here rather than in either caller because §5.11a's whole reason for this module is
	that a feed must not answer differently over two transports.

	"""

	if since is None:
		return

	if since > LAST_SEQ:
		raise _past_the_last_seq("since", since)

	if since < FIRST_SEQ:
		raise subroutine.errors.ValidationError(
			f"'since' is a seq and the first one is {FIRST_SEQ}, so {since} names nothing.",
			code="invalid_field_value",
			errors=[
				subroutine.errors.FieldError(
					field="since",
					code="invalid_field_value",
					message="Send the seq of the last event you processed, or omit 'since' "
					"to start from the oldest event still held.",
				)
			],
		)


def refuse_an_expired_cursor (
	session: sqlalchemy.orm.Session,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	since: int | None,
) -> None:
	"""Refuse a cursor after which events in the reader's workspaces have moved to the archive.

	§5.11 retains events for a configurable period and requires ``410 cursor_expired`` past that
	floor, so a client resyncs rather than being handed a page that silently omits what moved - the
	one failure a feed must never have, because it looks exactly like nothing having happened.

	**Per workspace, and only past the cursor** (`#4293`, M13 and NEW-C-1 of the cold review of
	2026-10-03, decision `#4305`). One number for the whole instance refused a quiet workspace's
	cursor when only other workspaces' events had moved, and refused a cursor naming the last event
	moved, which the client had already processed: ``since`` is inclusive, so nothing after it is
	lost. One lookup on the archive's ``(workspace_id, seq)`` index; the reader's workspaces rather
	than what each reader may see, which would scan every archived row past the cursor whenever none
	of it is visible.

	**Asked after the page, by both transports.** Asked before it, a run committing in between moved
	rows the page then never read - measured on PostgreSQL, rows 12 to 23 gone and no 410.
	"""

	if since is None:
		return

	archive = subroutine.db.models.activity.ARCHIVE
	through = session.scalar(
		sqlalchemy.select(sqlalchemy.func.max(archive.c.seq)).where(
			archive.c.workspace_id.in_(workspace_ids), archive.c.seq > since
		)
	)

	if through is None:
		return

	# **The hint names the number** (`#4298`, of the cold review of 2026-10-03, decision `#4305`).
	# It said to ask again without 'since', which on HTTP starts from the oldest event held, while
	# the agent tool sends no 'since' to mean the newest: one sentence, two different restarts.
	# ``since`` is inclusive, so the first event the feed still holds after the archive is N+1, and
	# it means the same on HTTP, the agent tool and the terminal.
	raise subroutine.errors.CursorExpired(
		f"Events up to seq {through} have been moved to the archive, so what happened since "
		f"{since} cannot be reported in full.",
		hint=f"Re-read what you rely on, then carry on from seq {through + 1}; the journal and each "
		"item's history still read what moved.",
	)


def refuse_a_period_behind_the_floor (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	since: int | None = None,
	before: int | None = None,
	mine: bool = False,
	by: uuid.UUID | None = None,
	newest: bool = False,
	narrowing: typing.Sequence[typing.Any] = (),
) -> None:
	"""Refuse a period whose events the feed no longer holds, naming the journal - `#4292`.

	M2 of the cold review of 2026-10-03, decision `#4305`. A period behind the retention floor -
	``created_at``, or a walk back with ``newest`` and ``before`` - answered ``200 []``, which
	reads as nothing having happened, while the journal read the same period. **Refused when the
	archive holds an event the request would have matched**, in the reader's workspaces, so a
	period the feed still holds whole is answered as before; and one that straddles the floor is
	refused outright rather than answered for its live half.

	**Asked after the page is read**, by both transports, so a run moving events in between cannot
	leave the page short with nothing said. Whether a request is a period is read off its own
	clauses rather than passed in, so neither caller can come to disagree about it.

	**A code of its own**, ``period_archived`` (decision `#4305`): its remedy is the journal, where
	``cursor_expired``'s is to carry on after the last event moved, so a client can tell them apart
	by the code alone.
	"""

	live = typing.cast(sqlalchemy.Table, subroutine.db.models.activity.Event.__table__)
	dated = any(
		getattr(element, "table", None) is live and getattr(element, "name", None) == "created_at"
		for clause in narrowing
		for element in sqlalchemy.sql.visitors.iterate(clause)
	)
	walking_back = newest and since is None and before is not None

	if not (dated or walking_back):
		return

	archive = subroutine.db.models.activity.ARCHIVE

	# **The request's own clauses, asked of the archive.** Written against the live table, and
	# re-pointed at the archive's columns of the same names - only the live table's, so a clause
	# reaching another table keeps its own.
	moved = sqlalchemy.sql.util.ClauseAdapter(
		archive,
		adapt_on_names=True,
		include_fn=lambda column: getattr(column, "table", None) is live,
	)
	statement = sqlalchemy.select(archive.c.seq).where(
		archive.c.workspace_id.in_(workspace_ids),
		*[moved.traverse(clause) for clause in narrowing],
	)

	if since is not None:
		statement = statement.where(archive.c.seq >= since)

	if before is not None:
		statement = statement.where(archive.c.seq < before)

	if by is not None:
		statement = statement.where(archive.c.actor_user_id == by)

	if mine:
		token = None if principal.token is None else principal.token.id
		statement = statement.where(
			sqlalchemy.false() if token is None else archive.c.actor_token_id == token
		)

	if session.scalar(statement.limit(1)) is None:
		return

	raise subroutine.errors.PeriodArchived(
		"Events in that period have been moved to the archive, so the change feed cannot report "
		"it in full.",
		hint="The journal reads every event, the archive's too: GET /v1/journal, 'subroutine "
		"journal' or subroutine_journal, with the same filter.",
	)


def _past_the_last_seq (field: str, asked: int) -> subroutine.errors.ValidationError:
	"""Refuse a ``seq`` larger than any the column can hold - `#3933`."""

	return subroutine.errors.ValidationError(
		f"{field!r} is a seq, and {asked} is larger than any seq can be.",
		code="invalid_field_value",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"A seq is a whole number from {FIRST_SEQ} to {LAST_SEQ}.",
			)
		],
	)


class Described(typing.NamedTuple):
	"""How an item is named to a reader, as against how it is keyed."""

	#: ``None`` for the things that carry no ref — a project, a workspace (§6.2).
	ref: int | None
	title: str

	#: What kind of item it is, as an id into the workspace's types — `#2727`. ``None`` for a
	#: project, which has no type.
	type_id: uuid.UUID | None = None

	#: Where it is filed: the project an item is in, or a project's own id. **An id rather than
	#: a path**, because a path is a walk up the tree and whoever renders it decides which
	#: projects its reader may be told about.
	project_id: uuid.UUID | None = None

	#: **How a task's dates are written, as it stands** — decision `#2823`. A journal writes a
	#: date in the item's own zone and as a whole day where it is one, and a change carries
	#: the flag only when the flag moved too. The rows are loaded here to name the item
	#: anyway, so these are columns rather than queries. Nothing else carries a date.
	timezone: str | None = None
	due_is_all_day: bool = False
	starts_is_all_day: bool = False
	snoozed_is_all_day: bool = False

	#: What a repeat counts from, which is half of reading its rule back as a sentence.
	recurrence_anchor: str | None = None


def descriptions (
	session: sqlalchemy.orm.Session,
	rows: typing.Sequence[subroutine.db.models.activity.Event],
) -> dict[uuid.UUID, Described]:
	"""Return the ref and title of whatever each of ``rows`` is *about*, keyed by that id.

	**"About" is the subject when there is one and the entity otherwise.** An event recording
	a comment names the comment as its entity, which has no ref and no title and is not what a
	reader wants to be told; its subject is the item somebody wrote on, which is. Deciding that
	here rather than in each client is what keeps a CLI, an agent and a future browser saying
	the same thing about the same row.

	**Batched, because the alternative is `#39`'s N+1 by another name.** Three queries per
	page whatever its size, and a page of fifty events touching one task asks about that task
	once.

	Anything unresolvable is simply absent from the map — a workspace, a link, an item hard to
	reach — and the view reports null rather than inventing a name. Nothing here re-checks
	visibility: these rows have already been narrowed by :func:`feed` or resolved through a
	subject, and an event a caller may read is one whose item they may read by construction.
	"""

	# **Every renderer of these rows asks for this batch first, so the texts come back here**
	# (`#578`): a long text an event keeps elsewhere is put back before anything shows the row.
	whole(session, rows)

	wanted: dict[str, set[uuid.UUID]] = {"task": set(), "document": set(), "project": set()}

	for row in rows:
		kind = row.subject_type or row.entity_type
		identifier = row.subject_id or row.entity_id

		if kind in wanted:
			wanted[kind].add(identifier)

	task = subroutine.db.models.work.Task
	document = subroutine.db.models.work.Document
	project = subroutine.db.models.project.Project
	found: dict[uuid.UUID, Described] = {}

	# Written out per model rather than looped over a tuple of them: the three do not share a
	# base that declares `ref` and `title`, and a loop only type-checks by widening to `Base`
	# and then reaching for attributes it cannot promise are there.
	#
	# **Columns rather than rows, for the two kinds that carry a text** (`#2764`, and the cold
	# review of 2026-09-18's M-9). A whole task is its description and a whole document is its
	# body - one body on the served instance was 134 KB when `#2764` was filed - and nothing
	# here shows either. Selecting the entity read every one of them for each page of the feed,
	# a journal or an item's history that named it, and threw it away. Same statements as
	# before, one per kind; narrower answers.
	if wanted["task"]:
		for one in session.execute(
			sqlalchemy.select(
				task.id, task.ref, task.title, task.type_id, task.project_id, task.timezone,
				task.due_is_all_day, task.starts_is_all_day, task.snoozed_is_all_day,
				task.recurrence_anchor,
			).where(task.id.in_(wanted["task"]))
		):
			found[one.id] = Described(
				ref=one.ref,
				title=one.title,
				type_id=one.type_id,
				project_id=one.project_id,
				timezone=one.timezone,
				due_is_all_day=one.due_is_all_day,
				starts_is_all_day=one.starts_is_all_day,
				snoozed_is_all_day=one.snoozed_is_all_day,
				recurrence_anchor=one.recurrence_anchor,
			)

	if wanted["document"]:
		for paper in session.execute(
			sqlalchemy.select(
				document.id, document.ref, document.title, document.type_id, document.project_id,
			).where(document.id.in_(wanted["document"]))
		):
			found[paper.id] = Described(
				ref=paper.ref,
				title=paper.title,
				type_id=paper.type_id,
				project_id=paper.project_id,
			)

	if wanted["project"]:
		for folder in session.scalars(
			sqlalchemy.select(project).where(project.id.in_(wanted["project"]))
		):
			found[folder.id] = Described(ref=None, title=folder.title, project_id=folder.id)

	return found


def jsonable (value: typing.Any) -> typing.Any:
	"""Convert a value into something the JSON column can hold.

	UUIDs and datetimes are the two that appear constantly and serialise nowhere by
	default. Anything else unrecognised becomes its string form rather than raising: an
	event that records a change imperfectly is worth more than a mutation that fails
	because its audit record could not be written.
	"""

	if value is None or isinstance(value, str | int | float | bool):
		return value

	if isinstance(value, uuid.UUID):
		return str(value)

	if isinstance(value, datetime.datetime | datetime.date):
		return value.isoformat()

	if isinstance(value, list | tuple | set | frozenset):
		return [jsonable(item) for item in value]

	if isinstance(value, dict):
		return {str(key): jsonable(item) for key, item in value.items()}

	return str(value)
