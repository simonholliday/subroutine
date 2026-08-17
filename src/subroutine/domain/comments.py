"""What happened, as opposed to what you concluded (SPEC.md §5.10).

**The distinction is the whole point of this module.** A comment is a running record — "ran the
suite, two failures, both in the date parser" — and a document is a conclusion — "the date
grammar treats a bare weekday as capture shorthand and a `due` field refuses it". If the next
session would need to read it, it is a document; if it is what you did, it is a comment.

Until this existed there was nowhere for the first kind. The `comment` table has been in the
schema since M1 and nothing wrote to it, so an agent working a task could either overwrite the
description — destroying what the task *is* in order to say what happened to it — or write a
document, which is far too heavy for a line about a test run. That gap was found by using the
product on its own plan.

Comments are **flat and chronological by decision**. ``parent_comment_id`` stays in the schema
as the escape hatch and is deliberately not exposed: threading is a feature of discussion tools,
and a work record reads better as a sequence of what happened than as a tree of who replied to
whom.
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.mixins
import subroutine.db.models.activity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.mentions
import subroutine.domain.patch
import subroutine.domain.scoping
import subroutine.domain.text
import subroutine.domain.versions
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions

#: What a comment may hang off. The same three §5.10 names, read from the schema so a fourth
#: cannot be added here without the CHECK constraint agreeing.
ENTITY_TYPES = subroutine.db.mixins.COMMENT_ENTITY_TYPES

#: Long enough for a paragraph of findings and short enough that nobody pastes a log file into
#: the work record. Checked here rather than left to the column, so the message names the field
#: and the limit — SQLite does not enforce a length at all.
MAX_BODY_LENGTH = 10_000


def _entity (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	*,
	entity_type: str,
	entity_id: uuid.UUID,
	writing: bool = True,
) -> typing.Any:
	"""Return the thing being commented on, or report that there is no such thing.

	Looked up **through the scoping helpers**, so an item the caller cannot see is reported as
	absent rather than forbidden — a comment endpoint that said "forbidden" about a task in a
	private project would confirm the task exists (§7.3a).

	**A deleted item resolves for a read and not for a write** (`#535`). ``api/tasks._resolve``
	already settled the reading half in as many words — *"Deleted tasks resolve. A reference to
	something in the trash is more useful than a dangling one"* — and this did not follow it, so
	``GET /v1/tasks/534`` answered **200** with the task while
	``GET /v1/tasks/534/comments`` answered **404**. One rule, two answers, which is this
	codebase's signature defect; and because ``subroutine show`` reads an item's comments, the
	visible result was that reading a deleted item failed in the words of the *comment* command
	nobody had run.

	The asymmetry belongs between reading and writing, not between an item and its record.
	Adding to the record of something in the trash is the thing to refuse, and refusing it by
	name — rather than by claiming the item does not exist — is what tells somebody they want
	``restore``.
	"""

	if entity_type not in ENTITY_TYPES:
		raise subroutine.errors.ValidationError(
			f"{entity_type!r} is not something that takes comments.",
			errors=[
				subroutine.errors.FieldError(
					field="entity_type",
					code="invalid_field_value",
					message=f"Expected one of: {', '.join(ENTITY_TYPES)}.",
				)
			],
		)

	models: dict[str, typing.Any] = {
		"task": subroutine.db.models.work.Task,
		"project": subroutine.db.models.project.Project,
		"document": subroutine.db.models.work.Document,
	}

	# `actor=None` is an unauthenticated internal caller — the same escape hatch every service
	# here has, legitimate for `bootstrap` and for tests. It skips *scoping*, not validation,
	# and `tests/test_actor_discipline.py` is what stops it becoming a silent hole.
	if actor is None:
		found = session.get(models[entity_type], entity_id)

	else:
		reachable = [
			space.id for space in subroutine.domain.workspaces.readable(session, actor)
		]

		if entity_type == "task":
			statement: typing.Any = subroutine.domain.scoping.readable_tasks(
				actor,
				workspace_ids=reachable,
				include_completed=True,
				include_deleted=True,
			).where(subroutine.db.models.work.Task.id == entity_id)

		elif entity_type == "project":
			statement = subroutine.domain.scoping.readable_projects(
				actor, workspace_ids=reachable, include_archived=True, include_deleted=True
			).where(subroutine.db.models.project.Project.id == entity_id)

		else:
			statement = subroutine.domain.scoping.readable_documents(
				actor, workspace_ids=reachable, include_archived=True, include_deleted=True
			).where(subroutine.db.models.work.Document.id == entity_id)

		found = session.scalars(statement).first()

	if found is None:
		raise subroutine.errors.NotFound(f"There is no {entity_type} here to comment on.")

	# **Deleted is refused for a write and allowed for a read**, and it is refused *by name*.
	# Reporting it as absent would be the same sentence a caller gets for something that never
	# existed, on the one occasion they know perfectly well it did — they deleted it.
	if writing and getattr(found, "deleted_at", None) is not None:
		raise subroutine.errors.ValidationError(
			f"That {entity_type} is in the trash, so nothing more can be added to its record.",
			hint="Restore it first if you meant to keep working on it.",
		)

	return found


def _project_of (
	session: sqlalchemy.orm.Session,
	subject: typing.Any,
	*,
	entity_type: str,
) -> subroutine.db.models.project.Project:
	"""Return the project a comment on this thing lands in.

	``comment:write`` is one of :data:`subroutine.permissions.WRITES_INSIDE_A_PROJECT`, so a
	credential's write set has to be asked about somewhere — and
	``authorization._refusal`` puts both scope checks behind *having* a project, which is why
	passing the workspace alone left the narrowing inert (`#940`).

	A task and a document each carry a project; a comment on a project lands in that project.
	Nothing else takes one.
	"""

	if entity_type == "project":
		return typing.cast(subroutine.db.models.project.Project, subject)

	found = session.get(subroutine.db.models.project.Project, subject.project_id)

	if found is None:
		# ``project_id`` is NOT NULL with a foreign key on both backends, so reaching here
		# means the schema is broken. The one thing not to do about it is fall through to
		# ``project=None``, which is precisely the permissive answer this function exists to
		# stop — an inert control, wearing the fix for one.
		raise subroutine.errors.NotFound(
			f"The project that {entity_type} belongs to could not be read."
		)

	return found


def _clean (body: str) -> str:
	"""Return a usable comment body, or refuse with a reason."""

	return subroutine.domain.text.fit(
		subroutine.domain.text.require(body, field="body"),
		field="body",
		limit=MAX_BODY_LENGTH,
		# **The one field here that is genuinely prose** (`#927` H-8). Everything else
		# `text.fit` sees is a title, a name or a slug, and those are one line by definition —
		# so the default collapses whitespace and this says out loud that a comment is the
		# exception. Somebody writing paragraphs meant to.
		multiline=True,
	)


def create (
	session: sqlalchemy.orm.Session,
	*,
	entity_type: str,
	entity_id: uuid.UUID,
	body: str,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.activity.Comment:
	"""Record what happened, against a task, a project or a document.

	The mention index is wired here and that is what makes this more than CRUD: writing "blocked
	by #42" in a comment makes the comment a backlink on #42, so somebody reading #42 sees that
	something is waiting on it without anyone having remembered to link them.

	The event names the comment as its entity and the commented-on item as its **subject**, which
	is what puts it in that item's history. Recording it against the item instead would be the
	shorter fix and a false one: the item's own row did not change, and ``#52`` settled that
	``updated_at`` should go on meaning exactly that.
	"""

	subject = _entity(session, actor, entity_type=entity_type, entity_id=entity_id)
	text = _clean(body)

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.COMMENT_WRITE,
			workspace_id=subject.workspace_id,
			# **The project, not the workspace alone** (`#940`). A credential issued
			# ``--write acme`` may read a related tree and change only its own project, and
			# adding to somebody else's record is changing it — which is why `#370` put
			# ``comment:write`` in the write set in the first place.
			project=_project_of(session, subject, entity_type=entity_type),
		)

	comment = subroutine.db.models.activity.Comment(
		id=subroutine.db.types.new_uuid(),
		workspace_id=subject.workspace_id,
		entity_type=entity_type,
		entity_id=entity_id,
		author_id=None if actor is None else actor.user.id,
		body=text,
	)
	session.add(comment)
	session.flush()

	subroutine.domain.mentions.synchronize(
		session,
		workspace_id=subject.workspace_id,
		source_type="comment",
		source_id=comment.id,
		texts=(text,),
	)

	subroutine.domain.events.record(
		session,
		workspace_id=subject.workspace_id,
		entity_type="comment",
		entity_id=comment.id,
		subject_type=entity_type,
		subject_id=entity_id,
		action=subroutine.domain.events.EventAction.CREATED,
		actor=actor,
	)
	session.flush()

	return comment


def listing (
	session: sqlalchemy.orm.Session,
	*,
	entity_type: str,
	entity_id: uuid.UUID,
	actor: subroutine.domain.authentication.Principal | None = None,
	include_deleted: bool = False,
) -> sqlalchemy.Select[typing.Any]:
	"""Return the statement for one item's comments, oldest first.

	**Oldest first, unlike every other listing here**, and deliberately: a work record is read
	as a story from the beginning, where a task list is read newest-first because the newest is
	the one you act on. The index on ``(workspace_id, entity_type, entity_id, created_at)`` is
	exactly this query.
	"""

	subject = _entity(
		session, actor, entity_type=entity_type, entity_id=entity_id, writing=False
	)

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.COMMENT_READ,
			workspace_id=subject.workspace_id,
		)

	model = subroutine.db.models.activity.Comment
	statement = sqlalchemy.select(model).where(
		model.workspace_id == subject.workspace_id,
		model.entity_type == entity_type,
		model.entity_id == entity_id,
	)

	if not include_deleted:
		statement = statement.where(model.deleted_at.is_(None))

	return statement.order_by(model.created_at.asc(), model.id.asc())


def get (
	session: sqlalchemy.orm.Session,
	comment_id: uuid.UUID,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.activity.Comment:
	"""Return one comment the caller may read, or report that there is no such thing."""

	model = subroutine.db.models.activity.Comment
	found = session.get(model, comment_id)

	if found is None:
		raise subroutine.errors.NotFound("There is no such comment.")

	# Reached through the *subject*, so a comment on something the caller cannot see is absent
	# rather than readable — the comment table has no visibility of its own.
	_entity(
		session,
		actor,
		entity_type=found.entity_type,
		entity_id=found.entity_id,
		writing=False,
	)

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.COMMENT_READ,
			workspace_id=found.workspace_id,
		)

	return found


def update (
	session: sqlalchemy.orm.Session,
	comment: subroutine.db.models.activity.Comment,
	*,
	body: str = subroutine.domain.patch.UNSET,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.activity.Comment:
	"""Edit a comment's text, rewriting whatever it mentions.

	**Only the author may edit**, whatever their role. A comment is attributed prose, and an
	administrator rewriting somebody else's words while leaving their name on them is not a
	permission anybody should hold — deleting it is the honest alternative and is allowed.
	"""

	_authored_by(actor, comment, verb="edit")
	subroutine.domain.versions.require(comment, expected_version, noun="This comment")

	if body is subroutine.domain.patch.UNSET:
		return comment

	text = _clean(body)

	if text == comment.body:
		return comment

	before = comment.body
	comment.body = text
	comment.version += 1

	subroutine.domain.mentions.synchronize(
		session,
		workspace_id=comment.workspace_id,
		source_type="comment",
		source_id=comment.id,
		texts=(text,),
	)

	subroutine.domain.events.record(
		session,
		workspace_id=comment.workspace_id,
		entity_type="comment",
		entity_id=comment.id,
		subject_type=comment.entity_type,
		subject_id=comment.entity_id,
		action=subroutine.domain.events.EventAction.UPDATED,
		changes=subroutine.domain.events.changes_between(
			{"body": before}, {"body": text}
		),
		actor=actor,
	)
	session.flush()

	return comment


def delete (
	session: sqlalchemy.orm.Session,
	comment: subroutine.db.models.activity.Comment,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.activity.Comment:
	"""Move a comment to the trash, and stop it mentioning anything.

	Soft, like everything else here — nothing in this system hard-deletes, which is what lets an
	event about a deleted thing still be scoped (§5.11a).

	The author may delete their own; ``comment:write`` plus workspace administration may delete
	anybody's, because a work record sometimes needs a mistake taken out of it.
	"""

	if actor is not None and comment.author_id != actor.user.id:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.WORKSPACE_ADMIN,
			workspace_id=comment.workspace_id,
		)

	subroutine.domain.versions.require(comment, expected_version, noun="This comment")

	if comment.deleted_at is not None:
		return comment

	comment.deleted_at = now if now is not None else subroutine.db.types.utcnow()
	# A delete is a change, so the version moves — see the rule this project learned the hard
	# way, where two of three deletes left `expected_version` silently passing for stale callers.
	comment.version += 1

	# The text is gone from view, so its backlinks go too. A backlink pointing at a sentence
	# nobody can read is worse than no backlink.
	subroutine.domain.mentions.synchronize(
		session,
		workspace_id=comment.workspace_id,
		source_type="comment",
		source_id=comment.id,
		texts=(),
	)

	subroutine.domain.events.record(
		session,
		workspace_id=comment.workspace_id,
		entity_type="comment",
		entity_id=comment.id,
		subject_type=comment.entity_type,
		subject_id=comment.entity_id,
		action=subroutine.domain.events.EventAction.DELETED,
		actor=actor,
	)
	session.flush()

	return comment


def _authored_by (
	actor: subroutine.domain.authentication.Principal | None,
	comment: subroutine.db.models.activity.Comment,
	*,
	verb: str,
) -> None:
	"""Refuse an edit by anybody but the author. ``None`` is an internal caller."""

	if actor is None or comment.author_id == actor.user.id:
		return

	raise subroutine.errors.Forbidden(
		f"Only the person who wrote a comment may {verb} it.",
		hint="Delete it instead if it needs to go; that is allowed for an administrator.",
	)
