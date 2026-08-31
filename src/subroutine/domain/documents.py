"""Specifications, designs, notes, decisions, findings and dead ends.

The sibling of a task, and the reason there are two entities rather than one (docs/design.md
§5.6): a bug is done or not done and carries an assignee, a deadline and an estimate; a
specification is never "done" — it is draft, then active, then superseded — and has an
owner rather than a worker. Half the columns differ, and splitting on that keeps both
models honest.

So there is deliberately **no** ``due_at``, ``starts_at``, ``estimate_minutes`` or
``assignee_id`` here. "The spec must be signed off by Friday" is a *task* of type ``chore``
that ``documents`` the spec, which keeps the deadline in the agenda where a deadline belongs
and means no scheduling query ever has to exclude a document.

What is shared is shared completely: the ref space, the project tree, permissions
(``task:*`` — a document is a work item under the same rules as the task beside it), the
event feed, and the mention index.
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.config
import subroutine.db.mixins
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.seed
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.mentions
import subroutine.domain.patch
import subroutine.domain.refs
import subroutine.domain.tags
import subroutine.domain.text
import subroutine.domain.users
import subroutine.domain.versions
import subroutine.errors
import subroutine.permissions

#: docs/design.md §6.10, matching tasks.
MAX_TITLE_LENGTH = 512

#: The status a document moves to when something supersedes it. A category rather than a
#: key, for the reason every other status lookup here uses one: an installation renames
#: them freely.
SUPERSEDED_CATEGORY = "superseded"

#: The status of a document that is in force — ``active``, by category (`#506`).
CURRENT_CATEGORY = "current"

#: The types that are true the moment they are written, and so start *active* rather than
#: *draft* (`#506`, Simon 2026-08-05; widened to every seeded type by `#537`, Simon
#: 2026-08-24).
#:
#: **The writing is the act, so a document is in force unless its author says otherwise.** A
#: decision that has been taken is already in force; a finding is already true; a dead end
#: already records that a route does not work; a note is already noted; and a specification is
#: *the* specification until something supersedes it. ``draft`` is a claim only the writer can
#: make, and ``status_key`` is how they make it.
#:
#: **This originally excluded ``spec``, ``design`` and ``note``, and the argument for that was
#: about the lifecycle rather than about the first state.** §6.14's lifecycle does fit a
#: specification — drafted, agreed, replaced — and that is all true; what it got wrong is where
#: the lifecycle starts. **Measured on the only instance with real documents on it: 76 of 78
#: were in force and 47 of those were labelled ``draft``.** A default that is wrong three times
#: in five is not a lifecycle, it is a trap, and its cost is not cosmetic — ``links.governing``
#: requires the *current* category, so a specification linked to the work it governs was absent
#: from the next reader's *Read first* while sitting plainly in its Links.
#:
#: **It also ends an asymmetry inside one category.** ``note`` and ``finding`` are both
#: ``record`` — what was observed — and only ``finding`` started in force.
#:
#: **Measured before it was changed**: all 72 open documents on this project's own instance
#: sat in ``draft``, including 26 decisions that plainly govern. A vocabulary that is
#: specified, seeded, published in ``/v1/meta`` and used by nothing is `#247`'s defect in a
#: fourth disguise, and the first one in *data* rather than in code.
#:
#: ``dead_end`` reads oddly here and is right: *this way does not work* is a conclusion in
#: force, not a draft of anything, and it is not superseded until somebody shows it was wrong.
#:
#: **This reverses a recorded decision, so here is the answer to it.** ``test_reach`` excused
#: setting a status at creation as *deliberate*, arguing that "a new document is a draft and
#: becomes something else by an act somebody took" — `#84`'s rule that a parent never
#: auto-completes, one entity along. That rule is about the system **inferring** a judgement
#: nobody made: completing the last child credits somebody with deciding the parent is done,
#: and it cannot reverse when a child is added later.
#:
#: Writing a decision is not an inference. **The act somebody took is the writing**, and the
#: status records it rather than concluding anything from it — so `#84`'s argument does not
#: reach this, and applying it here made the product unable to say the one thing a decision
#: document exists to say. A caller who *is* still drafting says so with ``status_key``, which
#: is why that had to become reachable from a client in the same change.
#:
#: **Derived from the seed rather than written out, and that is not the same as "always".** It
#: now covers every document type this program ships, so a hand-written copy would be a second
#: statement of the seed — the tell `#1157` recorded, where the guard saying the two agreed was
#: the evidence a copy had just been made. What it still discriminates is a type somebody
#: **added themselves** (`#826`), which falls through to the workspace's own default: we can say
#: what our six types mean and we cannot say what a ``proposal`` means, so deferring to the
#: vocabulary its author curated is more correct than assuming. That is also what keeps the
#: seeded ``is_default`` on ``draft`` from becoming a control nothing reaches.
#:
#: **A seventh seeded document type therefore starts in force by default**, and whoever adds one
#: should say here if it should not. ``db/seed.py`` carries a pointer back to this beside the
#: document types, so that decision is met rather than discovered.
#:
#: It reads *keys*, like ``documents.GOVERNS`` beside it, so a renamed type falls out of it —
#: which is `#1171`, tracked for the day `#1129` makes item types renameable and unchanged by
#: this widening.
IN_FORCE_WHEN_WRITTEN = frozenset(
	one.key for one in subroutine.db.seed.SEEDED_ITEM_TYPES if one.entity_type == "document"
)


class Governing (typing.NamedTuple):
	"""A document type that binds the next reader, and what it obliges them to.

	The sentence travels with the classification deliberately. *Why is this type in the set*
	and *what does a reader owe one of these* are the same question, so splitting them would
	let a seventh type join the set with nothing said about what following it means.
	"""

	#: The type key, as this installation seeds it.
	key: str

	#: The heading its section takes in the conventions index.
	heading: str

	#: What a reader owes a document of this type, in one line, rendered beneath the heading.
	obliges: str


#: The document types that **bind** somebody arriving here, in the order the conventions
#: index shows them (`#1036`).
#:
#: **Two questions were being answered by one field.** *Is this true yet* is answered by
#: ``status``, correctly, and `#506` settled it. *Must I follow it* was answered by nothing, so
#: ``subroutine://conventions`` guessed from ``type`` — which answers a third question, *what
#: kind of writing is this*. Measured on this project's own instance: six documents were in
#: force, governing, and excluded by the type filter alone, including the release procedure and
#: the accountability model.
#:
#: **A type cannot answer whether something is in force and never could**, which is why the
#: obvious fix — give ``spec`` and ``design`` an in-force default beside the three above — is
#: wrong. `#506` admits a type when it is *true the moment it is written*; a design is not.
#: `#445` carries eight open questions and is correctly a draft, while `#1023` records five
#: decisions taken and is incorrectly one. One type, both states, so only the status separates
#: them and this set says nothing about it.
#:
#: **``finding`` is deliberately out, and the cost is named rather than hidden** (Simon,
#: 2026-08-20). 37 of 39 findings in force here are code reviews, whose actionable half already
#: became items; a review describes, and its value is retrieval on demand rather than
#: always-on. The known cost is that `#927`'s *Not issues* section genuinely binds — *read it
#: before touching anything it covers* — so a reviewer may re-raise something already cleared.
#: The index keeps a pointer to findings and notes for that reason.
GOVERNING = (
	Governing(
		"decision",
		"What has been decided here",
		"Taken deliberately, with the alternatives weighed. Reopening one needs a reason the "
		"record does not already answer.",
	),
	Governing(
		"spec",
		"What has been specified here",
		"Agreed and written down, to be read rather than reconstructed. A procedure you "
		"reinvent is one you get subtly wrong.",
	),
	Governing(
		"design",
		"How things here were designed",
		"How something was settled before it was built, and why the alternatives were not "
		"taken. One still carrying open questions is a draft and is not listed.",
	),
	Governing(
		"dead_end",
		"What has been tried here and does not work",
		"Routes taken and closed. The reason a path is *not* taken leaves no trace in the "
		"code, so this is the only record that it was considered at all.",
	),
)

#: Derived from :data:`GOVERNING` rather than written twice, so the set a guard compares and
#: the sections a reader sees cannot drift apart.
GOVERNS = frozenset(one.key for one in GOVERNING)

#: The document types that **describe** rather than bind. Not a leftover: a type belongs to
#: exactly one of these two, so a seventh has to be classified rather than defaulting to
#: invisible — which is how six governing documents came to be missing from the one channel
#: that claims to name what binds you.
DESCRIBES = frozenset({"note", "finding"})


#: Every field :func:`update` compares, and so every field an ``updated`` event on a document
#: can name.
#:
#: **Declared rather than derived, unlike a task's**, and that asymmetry is the honest part: a
#: task's set is `tasks._snapshot`'s keys, read off a function whose whole job is to be complete,
#: while a document builds its changes as it goes. So this is a second statement of something
#: and can fall behind — which `tests/test_content_changes.py` closes in the direction that can
#: be closed, by driving every name here and insisting an event carries it. A field added to the
#: assignment pass and not to this list is caught by nothing, and is the reason to write the two
#: in the same edit.
#:
#: It exists at all so that `#1112`'s classification can be asked whether it is *complete*. A
#: rule with no list of what it has to cover cannot be incomplete, which is exactly how a
#: deadline stayed uncounted for the life of the column.
COMPARED: frozenset[str] = frozenset(
	{
		"title",
		"body",
		"owner_id",
		"status_id",
		"type_id",
		"supersedes_id",
		"tags",
		"project_id",
		# **Not a column.** `_retire` names the successor beside the status it is moving, so
		# that "why did this become superseded" is answerable from the event alone. It is here
		# because this list is about what an `updated` event can *say*, not about what a row
		# holds — and a classification that could not see it would be incomplete about the one
		# path that does not go through `update`.
		"superseded_by",
	}
)


def create (
	session: sqlalchemy.orm.Session,
	*,
	project: subroutine.db.models.project.Project,
	title: str,
	body: str | None = None,
	type_key: str = "note",
	status_key: str | None = None,
	parent: subroutine.db.models.work.Document | None = None,
	owner_id: uuid.UUID | None = None,
	supersedes: subroutine.db.models.work.Document | None = None,
	tags: typing.Sequence[str] | None = None,
	max_depth: int | None = None,
	settings: subroutine.config.Settings | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Document:
	"""Write a document into a project, allocating its ref and recording that it happened."""

	cleaned_title = _clean_title(title)
	body = subroutine.domain.text.readable(body, field="body")

	if owner_id is not None:
		# The same question, and this path asked nothing at all: an id naming nobody reached
		# the foreign key and left as an unhandled `IntegrityError`, which is a 500 for a
		# field a caller sent, and a real account outside the workspace was accepted.
		subroutine.domain.users.member(
			session, project.workspace_id, str(owner_id), field="owner_id"
		)

	if parent is not None and parent.project_id != project.id:
		raise subroutine.errors.ValidationError(
			"A section belongs to the same project as the document it is part of.",
			errors=[
				# **`parent`, not `parent_id`** (`#1534`). `documents.Create` accepts
				# `parent`, so a caller who did what this said was refused a second
				# time by `unknown_field` — `#1315`'s defect, one field along from the
				# dates `#1317` fixed.
				subroutine.errors.FieldError(
					field="parent",
					code="invalid_field_value",
					message="That document is in a different project.",
				)
			],
		)

	workspace_id = project.workspace_id

	_permitted(session, actor, subroutine.permissions.TASK_WRITE, project=project)

	item_type = item_type_for(session, workspace_id, type_key)
	status = (
		status_for(session, workspace_id, status_key)
		if status_key is not None
		else _first_status(session, workspace_id, type_key)
	)

	if supersedes is not None and supersedes.workspace_id != workspace_id:
		raise subroutine.errors.ValidationError(
			"A document can only supersede one in the same workspace.",
			errors=[
				subroutine.errors.FieldError(
					field="supersedes",
					code="invalid_field_value",
					message="That document belongs to a different workspace.",
				)
			],
		)

	ref = subroutine.domain.refs.allocate(session, workspace_id)

	document = subroutine.db.models.work.Document(
		id=subroutine.db.types.new_uuid(),
		workspace_id=workspace_id,
		project_id=project.id,
		parent_id=None if parent is None else parent.id,
		type_id=item_type.id,
		ref=ref,
		title=cleaned_title,
		body=body,
		status_id=status.id,
		owner_id=owner_id,
		supersedes_id=None if supersedes is None else supersedes.id,
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(document, parent, max_depth=subroutine.domain.hierarchy.depth_limit(max_depth, settings))

	session.add(document)
	session.flush()

	if tags:
		# After the flush, because the join row needs the document's id — and through `ensure`,
		# which is what holds §6.2's rule that a name of only digits is a reference rather than
		# a tag, however the tag arrived. The same call a task makes, against the same
		# workspace-scoped vocabulary (`#819`).
		subroutine.domain.tags.apply_to(
			session,
			document,
			subroutine.domain.tags.ensure(
				session, workspace_id=project.workspace_id, names=list(tags)
			),
		)

	if supersedes is not None:
		_retire(session, supersedes, document, actor=actor)

	subroutine.domain.mentions.synchronize(
		session,
		workspace_id=workspace_id,
		source_type="document",
		source_id=document.id,
		texts=(document.title, document.body),
	)

	subroutine.domain.events.record(
		session,
		workspace_id=workspace_id,
		entity_type="document",
		entity_id=document.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={"ref": {"from": None, "to": ref}, "title": {"from": None, "to": cleaned_title}},
		actor=actor,
	)
	session.flush()

	return document


def update (
	session: sqlalchemy.orm.Session,
	document: subroutine.db.models.work.Document,
	*,
	title: str = subroutine.domain.patch.UNSET,
	body: str | None = subroutine.domain.patch.UNSET,
	status_key: str = subroutine.domain.patch.UNSET,
	type_key: str = subroutine.domain.patch.UNSET,
	owner_id: uuid.UUID | None = subroutine.domain.patch.UNSET,
	project: subroutine.db.models.project.Project = subroutine.domain.patch.UNSET,
	supersedes: subroutine.db.models.work.Document | None = subroutine.domain.patch.UNSET,
	tags: typing.Sequence[str] | None = subroutine.domain.patch.UNSET,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Document:
	"""Change a document, recording only what actually changed.

	Anything left at ``UNSET`` is untouched; passing ``None`` clears the field (§8.3).
	**Everything is validated before anything is assigned**, for the reason ``tasks.update``
	gives: a rejected update must leave the row exactly as it was, because the caller holds a
	live session it may still commit.
	"""

	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_WRITE,
		project=session.get(subroutine.db.models.project.Project, document.project_id),
		workspace_id=document.workspace_id,
	)
	subroutine.domain.versions.require(document, expected_version, noun="This document")

	# **Filing it somewhere else** (`#294`). A document could be created into a project and
	# never moved, so a conclusion written before anybody decided where it belonged stayed in
	# the Inbox for good — and unlike a task's, a document's project is what decides *who can
	# read it* (§7.3a), so this was a permissions consequence rather than a tidiness one.
	moving = project is not subroutine.domain.patch.UNSET and project.id != document.project_id
	descendants: list[subroutine.db.models.work.Document] = []

	if moving:
		if project.workspace_id != document.workspace_id:
			# The same refusal `tasks.update` gives, for the same reason: a cross-workspace
			# move rewrites the ref's tenancy (§6.2) and would leave the document pointing at
			# another workspace's vocabulary. `#297` is the shape a real one takes.
			raise subroutine.errors.ValidationError(
				"A document cannot be moved to a project in another workspace.",
				errors=[
					subroutine.errors.FieldError(
						field="project",
						code="invalid_field_value",
						message=f"{project.key!r} is in a different workspace.",
						hint="Move it to a project in the same workspace, or write it there.",
					)
				],
			)

		# Both ends checked in the pass that may raise, so somebody who may write here and not
		# there cannot move a conclusion out of their own reach — nor learn it exists.
		_permitted(
			session,
			actor,
			subroutine.permissions.TASK_WRITE,
			project=project,
			workspace_id=project.workspace_id,
		)

		if document.parent_id is not None:
			# `create` refuses a section in a different project from the document it is part
			# of, so moving a child alone would break that invariant from the other side.
			raise subroutine.errors.ValidationError(
				"A section belongs to the same project as the document it is part of.",
				errors=[
					subroutine.errors.FieldError(
						field="project",
						code="invalid_field_value",
						message="This document is part of another, which decides its project.",
						hint="Move the one it belongs to — its sections go with it.",
					)
				],
			)

		descendants = list(
			session.scalars(
				sqlalchemy.select(subroutine.db.models.work.Document).where(
					subroutine.domain.hierarchy.subtree(
						subroutine.db.models.work.Document, document
					),
					subroutine.db.models.work.Document.id != document.id,
					subroutine.db.models.work.Document.deleted_at.is_(None),
				)
			)
		)

	# Validation pass. Nothing below this point may raise.
	cleaned_title: typing.Any = (
		subroutine.domain.patch.UNSET
		if title is subroutine.domain.patch.UNSET
		else _clean_title(title)
	)
	body = (
		body
		if body is subroutine.domain.patch.UNSET
		else subroutine.domain.text.readable(body, field="body")
	)
	status: typing.Any = (
		subroutine.domain.patch.UNSET
		if status_key is subroutine.domain.patch.UNSET
		else status_for(session, document.workspace_id, status_key)
	)

	# `#42`, and this is the half it was really about: a note becomes a decision once somebody
	# has read it and agreed, which is exactly when you find out what it was. Written as a
	# `note` and frozen as one is the commonest way a document ends up mis-filed.
	item_type: typing.Any = (
		subroutine.domain.patch.UNSET
		if type_key is subroutine.domain.patch.UNSET
		else item_type_for(session, document.workspace_id, type_key)
	)

	if (
		supersedes is not subroutine.domain.patch.UNSET
		and supersedes is not None
		and supersedes.id == document.id
	):
		raise subroutine.errors.Conflict(
			"A document cannot supersede itself.",
			code="cycle_detected",
			errors=[
				subroutine.errors.FieldError(
					field="supersedes",
					code="cycle_detected",
					message="That is this document.",
				)
			],
		)

	if owner_id is not subroutine.domain.patch.UNSET and owner_id is not None:
		# **Membership, not existence**, and it asked only about existence — so a document
		# could be handed to an account that is not in this workspace and cannot see it, and
		# the request was answered 201. `users.member` is the same question `assignee_for`
		# already asked properly about a task.
		subroutine.domain.users.member(
			session, document.workspace_id, str(owner_id), field="owner_id"
		)

	# Assignment pass.
	changes: dict[str, typing.Any] = {}
	previous_text = (document.title, document.body)

	for field, value in (
		("title", cleaned_title),
		("body", body),
		("owner_id", owner_id),
		("status_id", None if status is subroutine.domain.patch.UNSET else status.id),
		("type_id", None if item_type is subroutine.domain.patch.UNSET else item_type.id),
	):
		if value is subroutine.domain.patch.UNSET:
			continue

		# **Both of these carry `None` for "not asked", not for "clear it".** Neither is
		# nullable, so the loop's own `UNSET` test cannot speak for them — a resolved value is
		# an object and an unasked one is `None`, which is the one shape the loop above reads
		# as a value worth writing.
		if field == "status_id" and status is subroutine.domain.patch.UNSET:
			continue

		if field == "type_id" and item_type is subroutine.domain.patch.UNSET:
			continue

		existing = getattr(document, field)

		if existing == value:
			continue

		setattr(document, field, value)
		changes[field] = {"from": existing, "to": value}

	if supersedes is not subroutine.domain.patch.UNSET:
		wanted = None if supersedes is None else supersedes.id

		if document.supersedes_id != wanted:
			changes["supersedes_id"] = {"from": document.supersedes_id, "to": wanted}
			document.supersedes_id = wanted

	# **Tags replace rather than merge**, which is what §8.3 means by a field on a `PATCH` —
	# every other field here is assigned, and a `tags` that merged would be the only one a
	# caller could not use to *remove* anything. `None` clears, exactly as `[]` does.
	#
	# Compared by name before and after so the event carries a change somebody can read, and
	# so an update that re-sends the same tags records nothing — the rule `if not changes`
	# below depends on.
	if tags is not subroutine.domain.patch.UNSET:
		was = subroutine.domain.tags.names_on(session, document)
		subroutine.domain.tags.set_on(
			session,
			document,
			subroutine.domain.tags.ensure(
				session, workspace_id=document.workspace_id, names=list(tags or ())
			),
		)
		now = subroutine.domain.tags.names_on(session, document)

		if was != now:
			changes["tags"] = {"from": was, "to": now}

	if moving:
		# Captured before the assignment, because the descendants' events are recorded after
		# it and would otherwise report moving from the project they are moving *to*.
		came_from = document.project_id
		changes["project_id"] = {"from": came_from, "to": project.id}
		document.project_id = project.id

	if not changes:
		return document

	document.version += 1
	document.updated_by = None if actor is None else actor.user.id

	# **The same rule a task uses, from the same list** (`#1112`). This was written out here as
	# `"title" in changes or "body" in changes`, and `tasks.update` had a longer version of it
	# spelled a different way — so a document's status and type did not count while a task's
	# did, and neither disagreement was visible from inside either module.
	if subroutine.domain.events.touches_content("document", changes):
		document.content_updated_at = subroutine.db.types.utcnow()

	session.flush()

	if supersedes not in (subroutine.domain.patch.UNSET, None) and "supersedes_id" in changes:
		_retire(session, supersedes, document, actor=actor)

	if (document.title, document.body) != previous_text:
		subroutine.domain.mentions.synchronize(
			session,
			workspace_id=document.workspace_id,
			source_type="document",
			source_id=document.id,
			texts=(document.title, document.body),
		)

	subroutine.domain.events.record(
		session,
		workspace_id=document.workspace_id,
		entity_type="document",
		entity_id=document.id,
		action=subroutine.domain.events.EventAction.UPDATED,
		changes=changes,
		actor=actor,
	)

	# **Sections travel with the document they are part of, and each gets its own event.**
	# `create` holds the invariant that a section shares its parent's project, so a move that
	# left them behind would break it — and the history somebody reads is the *section's*, so
	# a count on the parent's event is not an answer to "what happened to this one" (§10.7
	# invariant 9). They are already loaded, so this writes no rows the move did not imply.
	for descendant in descendants:
		descendant.project_id = project.id
		descendant.version += 1

		subroutine.domain.events.record(
			session,
			workspace_id=descendant.workspace_id,
			entity_type="document",
			entity_id=descendant.id,
			action=subroutine.domain.events.EventAction.UPDATED,
			changes={"project_id": {"from": came_from, "to": project.id}},
			actor=actor,
		)

	session.flush()

	return document


def move (
	session: sqlalchemy.orm.Session,
	document: subroutine.db.models.work.Document,
	*,
	parent: subroutine.db.models.work.Document | None,
	max_depth: int | None = None,
	settings: subroutine.config.Settings | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> int:
	"""Re-nest a document and everything under it, returning how many rows were rewritten.

	**The worse half of `#44`, and it was worse for being invisible.** A document's
	``parent_id`` was reported by the view and accepted by no endpoint at all — not on create,
	not on update — so nesting existed in the schema and in every response and could not be
	reached from outside. A section of a specification could be read as belonging to its
	document and could never be made to.

	``parent=None`` makes it a top-level document. Everything else follows
	``tasks.move``, including refusing a parent in another project.
	"""

	filed_in = session.get(subroutine.db.models.project.Project, document.project_id)

	_permitted(
		session,
		actor,
		# `task:write`, like every other document write here — a document has no permission
		# of its own, which `#373` records as a deliberate not-yet rather than an oversight.
		subroutine.permissions.TASK_WRITE,
		project=filed_in,
		workspace_id=document.workspace_id,
	)

	subroutine.domain.versions.require(document, expected_version, noun="document")

	if parent is not None and parent.project_id != document.project_id:
		destination = session.get(subroutine.db.models.project.Project, parent.project_id)
		here = "another project" if filed_in is None else f"'{filed_in.key}'"
		there = None if destination is None else destination.key

		raise subroutine.errors.ValidationError(
			"A section belongs to the same project as the document it is part of.",
			errors=[
				subroutine.errors.FieldError(
					field="parent",
					code="invalid_field_value",
					message=(
						f"#{document.ref} is in {here} and #{parent.ref} is in "
						f"{'another project' if there is None else repr(there)}."
					),
					# **`document edit --project`, because there is no `doc move`** (`#1708`).
					# The word this named for months was borrowed from `subroutine move`, which
					# is a *task* command taking `--under` and `--top` — so a reader who acted
					# on it was refused a second time, about an unknown command rather than
					# about their document.
					#
					# **Both branches name the same remedy.** The other one said only *move it
					# there first* with no command at all, which leaves a reader exactly where
					# the broken one did; where the project cannot be seen from here we can
					# still say what to run, and only the key is theirs to supply.
					hint=(
						f"Move it there first, with 'subroutine document edit {document.ref} "
						f"--project {there}', then put it under #{parent.ref}."
						if there is not None
						else f"Move it into that project first, with 'subroutine document edit "
						f"{document.ref} --project <key>', then put it under #{parent.ref}."
					),
				)
			],
		)

	previous_parent = document.parent_id
	previous_path = document.path

	moved = subroutine.domain.hierarchy.reparent(
		session, subroutine.db.models.work.Document, document, parent, max_depth=subroutine.domain.hierarchy.depth_limit(max_depth, settings)
	)

	if moved == 0:
		return 0

	document.parent_id = None if parent is None else parent.id

	# The ETag argument from ``tasks.move``: the bulk path rewrite sets no version, so a
	# client holding one for a descendant could not tell that the descendant had moved.
	document.version += 1
	document.updated_by = None if actor is None else actor.user.id

	model = subroutine.db.models.work.Document
	session.execute(
		sqlalchemy.update(model)
		.where(
			model.workspace_id == document.workspace_id,
			subroutine.domain.hierarchy.subtree(model, document),
			model.id != document.id,
		)
		.values(version=model.version + 1, updated_by=document.updated_by)
		.execution_options(synchronize_session=False)
	)
	session.expire_all()
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=document.workspace_id,
		entity_type="document",
		entity_id=document.id,
		action=subroutine.domain.events.EventAction.MOVED,
		changes={
			"parent_id": {"from": previous_parent, "to": document.parent_id},
			"path": {"from": previous_path, "to": document.path},
			"descendants_rewritten": {"from": None, "to": moved - 1},
		},
		actor=actor,
	)
	session.flush()

	return moved


def delete (
	session: sqlalchemy.orm.Session,
	document: subroutine.db.models.work.Document,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Document:
	"""Move a document to the trash, where it stays recoverable (docs/design.md §6.9)."""

	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_DELETE,
		project=session.get(subroutine.db.models.project.Project, document.project_id),
		workspace_id=document.workspace_id,
	)
	subroutine.domain.versions.require(document, expected_version, noun="This document")

	if document.deleted_at is not None:
		return document

	document.deleted_at = now if now is not None else subroutine.db.types.utcnow()

	# **The version moves, because a delete is a change.** §8.9's promise is that a change is
	# based on the state you read, and a version that stands still across a soft delete breaks
	# it silently: read at v3, somebody trashes it, and `expected_version: 3` still passes — so
	# you edit a deleted item believing nothing happened. `projects.delete` did this and the
	# other two did not, which is what kept the gap invisible.
	document.version += 1
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=document.workspace_id,
		entity_type="document",
		entity_id=document.id,
		action=subroutine.domain.events.EventAction.DELETED,
		actor=actor,
	)
	session.flush()

	return document


def restore (
	session: sqlalchemy.orm.Session,
	document: subroutine.db.models.work.Document,
	*,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Document:
	"""Take a document back out of the trash — ``tasks.restore``'s counterpart (docs/design.md §6.9).

	Both, because one ref counter serves both kinds (§6.2) and ``show`` takes either, so a
	restore that worked on half the numbers would be a surprise nobody could predict from the
	ref they were holding.
	"""

	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_DELETE,
		project=session.get(subroutine.db.models.project.Project, document.project_id),
		workspace_id=document.workspace_id,
	)
	subroutine.domain.versions.require(document, expected_version, noun="This document")

	if document.deleted_at is None:
		return document

	# **Something else may supersede what this superseded**, and the index saying only one
	# thing can is partial — it ignores deleted rows, which is what allowed the second one to
	# be written. So a document deleted, replaced and then restored met the constraint at
	# flush time and left as an unhandled `IntegrityError`: a 500 over HTTP and a bare
	# traceback at the terminal, from three ordinary commands. `projects.restore` carries the
	# same check about a key.
	if document.supersedes_id is not None:
		model = subroutine.db.models.work.Document
		instead = session.scalars(
			sqlalchemy.select(model.ref).where(
				model.supersedes_id == document.supersedes_id,
				model.id != document.id,
				model.deleted_at.is_(None),
			)
		).first()

		if instead is not None:
			superseded = session.get(model, document.supersedes_id)
			replaced = "" if superseded is None else f" {subroutine.domain.refs.format_ref(superseded.ref)}"

			raise subroutine.errors.Conflict(
				f"Something else supersedes{replaced} now.",
				code="duplicate_key",
				errors=[
					# **`supersedes`, and this is the one site where no name is sendable**
					# (`#1534`). A restore takes no body, so neither the column nor the
					# request field can be put in one — but a reader who has met this
					# document has met `supersedes` on `create` and on `update`, and has
					# never seen `supersedes_id` anywhere. Naming the vocabulary word says
					# *which* thing conflicts; naming the column says nothing and looks
					# like a parameter they failed to find. The remedy is in the hint
					# below, which is where it has to be when there is no field to change.
					subroutine.errors.FieldError(
						field="supersedes",
						code="duplicate_key",
						message=f"{subroutine.domain.refs.format_ref(instead)} was written "
						f"while this was in the trash, and a document can be superseded once.",
					)
				],
				hint=f"Throw {subroutine.domain.refs.format_ref(instead)} away, or leave this "
				f"one where it is.",
			)

	document.deleted_at = None
	document.version += 1
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=document.workspace_id,
		entity_type="document",
		entity_id=document.id,
		action=subroutine.domain.events.EventAction.RESTORED,
		actor=actor,
	)
	session.flush()

	return document


def status_for (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str | None
) -> subroutine.db.models.vocabulary.Status:
	"""Return a document status by key, or the workspace's default when none is named."""

	return typing.cast(
		subroutine.db.models.vocabulary.Status,
		_vocabulary(
			session,
			subroutine.db.models.vocabulary.Status,
			workspace_id,
			key,
			field="status",
			noun="status",
		),
	)


def statuses_in_category (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, category: str
) -> list[uuid.UUID]:
	"""Return the ids of every document status in one category, for a listing to narrow by.

	**The twin of :func:`subroutine.domain.tasks.statuses_in_category`, and it had to exist**
	(`#1087`). A status *key* is a workspace's own and renameable; ``category`` is the fixed
	field beside it, published so a client may branch on it (§5.5). Without this a caller
	asking *which documents are in force here* had to name keys — and the keys are exactly the
	thing an installation may have changed, so the honest question could not be asked at all.

	**Measured before it was built**: `#1036` found an installation that renamed ``active``,
	and the whole of what binds an agent became a protocol error reading *there is no document
	status called 'active' here* — because both transports refuse an unknown key by name. The
	workaround was a client reading ``/v1/meta``, filtering by category and sending the keys
	back, which is a copy of a rule the server should be answering (`#925`).

	A task's categories are refused here by name, as a document's are on the task side. They
	are different vocabularies for a good reason — a superseded specification is not "done" —
	and passing one to the wrong listing is a mistake worth being told about rather than an
	empty page.
	"""

	if category not in subroutine.db.mixins.DOCUMENT_STATUS_CATEGORIES:
		known = ", ".join(subroutine.db.mixins.DOCUMENT_STATUS_CATEGORIES)

		raise subroutine.errors.ValidationError(
			f"{category!r} is not a status category a document can be in.",
			errors=[
				subroutine.errors.FieldError(
					field="status_category",
					code="invalid_field_value",
					message=f"No document status category called {category!r}.",
					hint=f"A document is in one of: {known}.",
				)
			],
		)

	model = subroutine.db.models.vocabulary.Status

	return list(
		session.scalars(
			sqlalchemy.select(model.id).where(
				model.workspace_id == workspace_id,
				model.entity_type == "document",
				model.category == category,
			)
		)
	)


def item_type_for (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str | None
) -> subroutine.db.models.vocabulary.ItemType:
	"""Return a document type by key, or the workspace's default when none is named."""

	return typing.cast(
		subroutine.db.models.vocabulary.ItemType,
		_vocabulary(
			session,
			subroutine.db.models.vocabulary.ItemType,
			workspace_id,
			key,
			field="type",
			noun="type",
		),
	)


def _vocabulary (
	session: sqlalchemy.orm.Session,
	model: typing.Any,
	workspace_id: uuid.UUID,
	key: str | None,
	*,
	field: str,
	noun: str,
) -> typing.Any:
	"""Look one vocabulary row up by key, or fall back to the workspace's default.

	One function for both tables because the two lookups differ only in which table they
	read: both are workspace-scoped, both carry an ``entity_type`` discriminator, and both
	have to name the valid alternatives when they fail (docs/design.md §5.5).
	"""

	statement = sqlalchemy.select(model).where(
		model.workspace_id == workspace_id, model.entity_type == "document"
	)

	if key is None:
		found = session.scalars(
			statement.where(model.is_default.is_(True)).order_by(model.position)
		).first()

	else:
		found = session.scalars(statement.where(model.key == key)).one_or_none()

	if found is not None:
		return found

	available = sorted(
		session.scalars(
			sqlalchemy.select(model.key).where(
				model.workspace_id == workspace_id, model.entity_type == "document"
			)
		)
	)

	raise subroutine.errors.ValidationError(
		f"This workspace has no default document {noun}."
		if key is None
		else f"There is no document {noun} called {key!r} here.",
		code="invalid_status" if noun == "status" else "invalid_field_value",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="not_found",
				message=f"No document {noun} with key {key!r} exists in this workspace.",
				hint=f"Valid keys here: {', '.join(available)}."
				if available
				else "This workspace's vocabulary is incomplete.",
			)
		],
	)


def _first_status (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, type_key: str
) -> subroutine.db.models.vocabulary.Status:
	"""Return the status a new document of this type starts in (`#506`).

	:data:`IN_FORCE_WHEN_WRITTEN` says which types are true as soon as somebody writes them;
	everything else starts wherever the workspace's default is, which is ``draft`` as seeded.

	**By category, and falling back rather than refusing.** An installation may rename or
	remove its statuses (§5.5), so this asks for the one meaning *in force* rather than for a
	key — and a workspace that has removed it gets the ordinary default instead of a refusal.
	Writing a decision must not fail because somebody edited a vocabulary; that is :func:`_retire`'s
	reasoning applied to the other end of the same lifecycle.
	"""

	if type_key not in IN_FORCE_WHEN_WRITTEN:
		return status_for(session, workspace_id, None)

	model = subroutine.db.models.vocabulary.Status
	found = session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == workspace_id,
			model.entity_type == "document",
			model.category == CURRENT_CATEGORY,
		)
		.order_by(model.position)
	).first()

	return found if found is not None else status_for(session, workspace_id, None)


def _retire (
	session: sqlalchemy.orm.Session,
	superseded: subroutine.db.models.work.Document,
	by: subroutine.db.models.work.Document,
	*,
	actor: subroutine.domain.authentication.Principal | None,
) -> None:
	"""Move a superseded document to the status that says so (docs/design.md §6.14).

	Done here rather than left to the caller because the two facts are one fact: a document
	that has been superseded and still reads as ``active`` is a document somebody will act
	on. If the workspace has removed its ``superseded`` status, the link stands and the
	status does not move — an installation is allowed to edit its own vocabulary, and
	refusing the whole operation over it would be worse.
	"""

	model = subroutine.db.models.vocabulary.Status
	replacement = session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == superseded.workspace_id,
			model.entity_type == "document",
			model.category == SUPERSEDED_CATEGORY,
		)
		.order_by(model.position)
	).first()

	if replacement is None or superseded.status_id == replacement.id:
		return

	previous = superseded.status_id
	superseded.status_id = replacement.id
	superseded.version += 1

	# **The one path that changes a document's status without going through `update`**, so it
	# is the one place the content rule has to be applied a second time (`#1112`). A decision
	# that has stopped being in force means something different to anybody reading it, and
	# this is where it stops.
	changes = {
		"status_id": {"from": previous, "to": replacement.id},
		"superseded_by": {"from": None, "to": by.ref},
	}

	if subroutine.domain.events.touches_content("document", changes):
		superseded.content_updated_at = subroutine.db.types.utcnow()

	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=superseded.workspace_id,
		entity_type="document",
		entity_id=superseded.id,
		action=subroutine.domain.events.EventAction.UPDATED,
		changes=changes,
		actor=actor,
	)


def _permitted (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	permission: str,
	*,
	project: subroutine.db.models.project.Project | None = None,
	workspace_id: uuid.UUID | None = None,
) -> None:
	"""Check that an actor may do this, or raise. ``None`` is an internal caller.

	See ``domain.tasks._permitted`` for why the ``None`` case is a skip and what stops it
	being a silent hole.
	"""

	if actor is None:
		return

	scope = workspace_id if project is None else project.workspace_id

	if scope is None:
		raise ValueError("A workspace or a project is needed to check a permission against.")

	subroutine.domain.authorization.authorize(
		session, actor, permission, workspace_id=scope, project=project
	)


def _clean_title (title: str) -> str:
	"""Return a usable document title, or refuse with a reason."""

	return subroutine.domain.text.fit(
		subroutine.domain.text.require(title, field="title"),
		field="title",
		limit=MAX_TITLE_LENGTH,
	)
