"""Recording what was checked against a task, and which tree it was checked on.

docs/design.md §14.5's evidence half, built as `#1121` and shaped by `#1124` Q6.

**A record, not a proof.** An agent can post an exit code of zero without having run anything,
so what this is worth is being *durable*, *attributable* and *invalidatable* — never *verified
work*. `#593` settled that sentence and it is repeated on the endpoint verbatim, because the
one way this feature fails is by being read as a guarantee.

**Bound to the tree rather than to the ticket.** §14.5 measured staleness against
``task.content_updated_at``, and that column does not move when the *code* does: run the suite
at 14:00, edit five files at 14:05, complete at 14:10, and the evidence is fresh by that
definition and false in fact. `#749` and `#893` are two releases this project published nothing
from for exactly that reason, and `#894`'s remedy — gate the commit the script makes — is this
rule one table along.
"""

import datetime

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.errors
import subroutine.permissions

#: How much output a record may carry. Enough to judge a summary by and not enough to be a log:
#: what this table is for is *what was checked*, and a caller wanting the run itself has a CI
#: system. Capped in the service so the refusal can say the limit rather than the database
#: quoting a constraint back.
MAX_OUTPUT = 8_192

#: How long a summary may be. The same bound a title has, for the same reason: it is one line
#: somebody reads in a list, and a paragraph there is a body in the wrong field.
MAX_SUMMARY = 512

#: How long a hash may be. Wide enough for SHA-256 (64 hex characters), so a repository that
#: has moved off SHA-1 records the same way — git's own transition, and a column that fitted
#: only the old one would be a decision about somebody else's repository.
MAX_HASH = 64


def record (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	passed: bool,
	summary: str | None = None,
	output_excerpt: str | None = None,
	tree_hash: str | None = None,
	commit_sha: str | None = None,
	ran_at: datetime.datetime | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Verification:
	"""Record what was checked against one task.

	**``task:write``, not ``task:read``.** Posting evidence against somebody's work is
	changing what that work says about itself, and a credential that may only look at a task
	should not be able to attach a claim to it.

	**A failing record is kept and is the more useful half of the pair.** *This was tried and
	did not work* is what stops it being tried again — and a table that held only successes
	would be a table nobody could learn from.

	**Nothing is edited afterwards.** A wrong record is answered by a later one, which is the
	same reasoning `#52` records about the event table: a record of what was checked at a
	moment is not a thing to rewrite.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.TASK_WRITE,
			workspace_id=task.workspace_id,
			project=session.get(subroutine.db.models.project.Project, task.project_id),
		)

	written = subroutine.db.models.work.Verification(
		id=subroutine.db.types.new_uuid(),
		workspace_id=task.workspace_id,
		task_id=task.id,
		passed=passed,
		summary=_within(summary, limit=MAX_SUMMARY, field="summary"),
		output_excerpt=_within(output_excerpt, limit=MAX_OUTPUT, field="output_excerpt"),
		tree_hash=_hash(tree_hash, field="tree_hash"),
		commit_sha=_hash(commit_sha, field="commit_sha"),
		ran_at=ran_at or subroutine.db.types.utcnow(),
		created_by=None if actor is None else actor.user.id,
	)
	session.add(written)
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=task.workspace_id,
		entity_type="verification",
		entity_id=written.id,
		# The task it is about, so the row can be scoped and so it reaches that item's
		# history — the pair `domain.comments` and `domain.links` already use.
		subject_type="task",
		subject_id=task.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={"passed": passed},
		actor=actor,
	)
	session.flush()

	return written


def against (
	task: subroutine.db.models.work.Task,
) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Verification]]:
	"""Return the statement for one task's records, newest first.

	**Newest first, unlike a comment thread.** A record is not read as a story: what a caller
	wants is the most recent thing that was checked, and everything older is context for it.
	"""

	model = subroutine.db.models.work.Verification

	return (
		sqlalchemy.select(model)
		.where(model.workspace_id == task.workspace_id, model.task_id == task.id)
		.order_by(model.ran_at.desc(), model.id.desc())
	)


def is_stale (
	written: subroutine.db.models.work.Verification, *, tree_hash: str | None
) -> bool | None:
	"""Say whether a record has expired against the tree the caller is standing on.

	**Three answers, not two**, and the third is why this returns ``None`` rather than
	``False``. A record with no tree hash cannot expire and must not read as fresh: it was made
	from a machine with no checkout, which §1.4 requires to be possible, and a caller with no
	tree of its own cannot judge one that has. *Unknown* is the honest answer and it is
	different from *current*.

	**The caller supplies the tree, because the server has none.** §10.7 invariant 11 says
	``is_stale`` is derived and never stored, and it is — but the thing it is derived *from*
	is not on this row and is not on the instance either. Only somebody standing in the
	checkout can answer it, which is why the comparison lives here and the value arrives from
	outside rather than being computed.
	"""

	if written.tree_hash is None or tree_hash is None:
		return None

	return written.tree_hash != tree_hash


def _within (value: str | None, *, limit: int, field: str) -> str | None:
	"""Refuse a value longer than this table will carry, naming the field and the limit."""

	if value is None:
		return None

	cleaned = value.strip()

	if not cleaned:
		return None

	if len(cleaned) <= limit:
		return cleaned

	raise subroutine.errors.ValidationError(
		f"That {field} is {len(cleaned)} characters and the limit is {limit}.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"At most {limit} characters.",
				hint="A record says what was checked; the run itself belongs where it ran.",
			)
		],
	)


def _hash (value: str | None, *, field: str) -> str | None:
	"""Refuse anything that is not a hexadecimal object name.

	**Checked rather than trusted, because this is compared for equality later.** A value with
	a stray newline or a `git rev-parse` error message in it would never match anything and the
	record would read as permanently stale, which is a wrong answer that looks like a right one.
	"""

	if value is None:
		return None

	cleaned = value.strip().lower()

	if not cleaned:
		return None

	if len(cleaned) <= MAX_HASH and all(letter in "0123456789abcdef" for letter in cleaned):
		return cleaned

	raise subroutine.errors.ValidationError(
		f"That {field} is not an object name.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"Hexadecimal, at most {MAX_HASH} characters.",
				hint="'git rev-parse HEAD^{tree}' prints one.",
			)
		],
	)
