"""Who answers for what an agent does, and the rule that the answer is always a person.

An agent is not a principal anybody can blame. Somebody gave it permission to work, and that
somebody is accountable for the result — which is decision ``#473``, and the reason
``user.responsible_user_id`` exists. **Accountability is a property of the agent rather than of
any task**: "Simon is responsible for this agent" does not vary per ticket, so it is one column
on the account and not a field every item has to carry.

Two rules make it worth anything, and the second is the one that fails quietly if it is missing.

**The chain terminates at a person.** Following ``responsible_user_id`` from any service account
reaches somebody who answers for themselves, in finite steps and without a cycle. A chain that
loops, or that ends at an agent, is an accountability gap that looks exactly like a working one
— every row is populated and every foreign key resolves.

**It is inherited, never chosen.** An agent that creates a sub-agent becomes the link that
sub-agent answers to, so the chain records the delegation *path* — sub-agent to agent to person
— rather than collapsing to whoever is ultimately on the hook. Letting the creator *name*
somebody instead would launder accountability in a single call: the sub-agent does something wrong and the trace terminates at
a person who authorised none of it. That is the shape ``_refuse_amplification`` exists for —
``#356`` found expiry was a fourth way to widen a credential, under a docstring asserting there
were three and all three were refused — and a creation path that improves the creator's own
position is an amplification whether what moves is a scope, an expiry, or a name on this chain.

A **person** with ``instance:user_create`` may name somebody else, because that is a person
taking responsibility for a delegation, which is the thing this models.
"""

import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.errors

#: How far a chain may be walked before it is treated as broken rather than long. A real one is
#: one or two links; anything approaching this is a cycle the write-time guard failed to stop,
#: and looping forever inside an authentication path is the worse of the two failures.
MAX_DEPTH = 16


def chain (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> list[subroutine.db.models.identity.User]:
	"""Return the accountability chain from ``user`` to the person who answers for it.

	The first entry is ``user`` itself and the last is a person. Raises when the chain does not
	reach one — because it loops, because a link is missing, or because it runs longer than
	:data:`MAX_DEPTH`.

	A person answers for themselves, so their chain is one entry long whatever
	``responsible_user_id`` happens to say.
	"""

	walked: list[subroutine.db.models.identity.User] = [user]
	seen: set[uuid.UUID] = {user.id}
	current = user

	while current.is_service_account:
		if current.responsible_user_id is None:
			raise subroutine.errors.ValidationError(
				f"No one is accountable for the agent '{current.username}'. An agent works on "
				f"somebody's behalf, so it needs a person who answers for it."
			)

		following = session.get(
			subroutine.db.models.identity.User, current.responsible_user_id
		)

		if following is None:
			raise subroutine.errors.ValidationError(
				f"The account answerable for the agent '{current.username}' no longer exists, "
				f"so nothing it does can be traced to a person."
			)

		if following.id in seen or len(walked) >= MAX_DEPTH:
			named = " → ".join(entry.username for entry in walked)

			raise subroutine.errors.ValidationError(
				f"Responsibility for '{user.username}' runs in a circle and never reaches a "
				f"person: {named} → {following.username}."
			)

		walked.append(following)
		seen.add(following.id)
		current = following

	return walked


def answers_for (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> subroutine.db.models.identity.User:
	"""Return the person accountable for ``user``, which is ``user`` itself for a person."""

	return chain(session, user)[-1]


def agents_answering_to (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> list[subroutine.db.models.identity.User]:
	"""Return the live service accounts this person answers for, directly or through another.

	Used to say *what will stop* before somebody is marked as having left — `project rename`'s
	precedent, which counts the items and names the three things that stop working before doing
	any of it. A deactivation that silently kills a shared agent is how a governance control
	comes to be routed around.

	Walks outward level by level rather than recursively, because the chain is a tree and the
	depth is small; :data:`MAX_DEPTH` bounds it for the same reason :func:`chain` does.
	"""

	model = subroutine.db.models.identity.User
	found: dict[uuid.UUID, subroutine.db.models.identity.User] = {}
	frontier = [user.id]

	for _step in range(MAX_DEPTH):
		if not frontier:
			break

		rows = list(
			session.scalars(
				sqlalchemy.select(model).where(
					model.responsible_user_id.in_(frontier),
					model.is_service_account.is_(True),
					model.deleted_at.is_(None),
				)
			)
		)

		# A cycle would otherwise walk for ever here. `chain` refuses one on the way in, so
		# reaching this is a database somebody edited — worth surviving rather than trusting.
		fresh = [row for row in rows if row.id not in found]

		for row in fresh:
			found[row.id] = row

		frontier = [row.id for row in fresh]

	return sorted(found.values(), key=lambda row: row.username)


def inherited (actor: subroutine.db.models.identity.User) -> uuid.UUID | None:
	"""Return who a *new* account created by ``actor`` must be answerable to: ``actor`` itself.

	**The creator, not the creator's person.** An agent that spawns a sub-agent becomes the link
	the sub-agent answers to, so the chain records the delegation *path* — sub-agent to agent to
	person — rather than collapsing it to whoever is ultimately on the hook. Both answer "who is
	accountable", because :func:`answers_for` walks to the end either way; only the nested form
	also answers "who handed this down", and that is the question decision `#473` is about.

	Written flat first — returning ``actor.responsible_user_id`` for an agent — and that was
	wrong in a way no unit test noticed: every chain was two links long, so deactivating an
	intermediate agent left everything it had created working, with nobody having decided that.
	Found by a test asserting the opposite and failing.

	It is still inherited rather than chosen, which is the property that matters: the creator is
	the creator, and nothing about this is settable.
	"""

	return actor.id


def refuse_an_unaccountable_agent (
	session: sqlalchemy.orm.Session,
	*,
	actor: subroutine.db.models.identity.User | None,
	is_service_account: bool,
	responsible_user_id: uuid.UUID | None,
) -> uuid.UUID | None:
	"""Return the responsible account for a new agent, refusing anything unaccountable.

	``actor`` is ``None`` for :mod:`subroutine.domain.bootstrap`, which runs before any principal
	exists and creates the first person — who is accountable for themselves and needs nothing
	here.

	**There is deliberately no permission check.** Creating any account already requires
	``instance:user_create``, so a caller who has reached this has it, and a second check against
	the same verb would be a branch nothing could ever take — which is the defect this repository
	keeps finding rather than a belt beside a brace. What remains is the rule a permission cannot
	express: an *agent* may not choose, however privileged its credential.
	"""

	if not is_service_account:
		# A person answers for themselves. Storing anybody else here would say otherwise, and
		# nothing reads it for a person, so a value would be a claim nothing enforces.
		return None

	# **The requirement is that somebody answers, not that somebody was authenticated.** An
	# explicit name satisfies it whoever is asking — which is what lets a fixture, an importer
	# or a migration create an accountable agent without inventing a principal to do it as.
	wanted = responsible_user_id

	if wanted is None:
		if actor is None:
			raise subroutine.errors.ValidationError(
				"An agent cannot be created without a person to answer for it. Say who is "
				"responsible for it, or create it as somebody."
			)

		wanted = inherited(actor)

		if wanted is None:
			raise subroutine.errors.ValidationError(
				f"No one is accountable for '{actor.username}', so it cannot create an agent "
				f"that would be answerable to nobody."
			)

	elif actor is not None and actor.is_service_account and wanted != inherited(actor):
		raise subroutine.errors.ValidationError(
			f"An agent cannot choose who answers for the agents it creates. "
			f"'{actor.username}' answers to somebody, and so does anything it makes."
		)

	named = session.get(subroutine.db.models.identity.User, wanted)

	if named is None:
		raise subroutine.errors.ValidationError(
			"The account named as answerable for this agent does not exist."
		)

	# Walking from the *named* account proves the new agent's chain before it is written: if the
	# person named is themselves an agent, their chain has to reach somebody, and this is the
	# only moment where refusing costs nothing.
	chain(session, named)

	return wanted
