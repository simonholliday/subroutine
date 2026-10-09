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

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.errors
import subroutine.permissions

#: How long a chain may be before it is treated as broken rather than long. A real one is one or
#: two links; anything approaching this is a cycle the write-time guard failed to stop, or a
#: nesting nobody meant, and looping forever inside an authentication path is the worse of the
#: two failures. **Held on the way in as well** (`#3939`): an agent is not made, or handed on,
#: where its chain, or the chain of an agent below it, would be longer than this.
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

		if following.id in seen:
			named = " → ".join(entry.username for entry in walked)

			raise subroutine.errors.ValidationError(
				f"Responsibility for '{user.username}' runs in a circle and never reaches a "
				f"person: {named} → {following.username}."
			)

		# **Too long is not a circle** (`#3939`), and was said as one: sixteen nested agents that
		# reached a person were refused as a loop, over a list that ended at the person.
		if len(walked) >= MAX_DEPTH:
			named = " → ".join(entry.username for entry in walked)

			raise subroutine.errors.ValidationError(
				f"The chain of responsibility for '{user.username}' is more than {MAX_DEPTH} "
				f"accounts long, which is treated as broken: {named} → {following.username}."
			)

		walked.append(following)
		seen.add(following.id)
		current = following

	return walked


#: Why an account cannot act: it has been deactivated, or it is an agent that nobody active answers
#: for. The two have different remedies - reactivate the account, or hand the agent to somebody - so
#: a door that refuses says which.
INACTIVE = "inactive"
UNANSWERED = "unanswered"

#: Why an account cannot act *in one workspace*: it, or the person at the top of its chain, is not a
#: member there (`#4546`, decision `#4518`).
OUTSIDE = "outside"


def standing (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	*,
	leaving: typing.Collection[uuid.UUID] = (),
	workspace_id: uuid.UUID | None = None,
) -> str | None:
	"""Say why this account cannot act, or ``None`` when it can - `#4545`, R1 of `#4506`.

	**The one rule, asked at every door and of every account an act names.** An account acts when
	it is live and active, and an agent only while every account it answers to is too - decision
	`#473`: when the person who gave an agent permission leaves, the permission goes with them. A
	chain that cannot be walked is a refusal, because an agent nobody answers for is what the model
	exists to prevent. It was spelled twelve ways, two of which missed the chain (G13 and G8 of the
	cold review of 2026-10-05); this and :func:`live` are where a user's ``deleted_at``, which
	nothing sets, is still read as standing.

	``leaving`` asks the same question of a future in which those accounts have gone, which is
	what lets somebody be told what a deactivation will strand before they make it.

	**``workspace_id`` asks it inside one workspace** (`#4546`, decision `#4518`): there the account
	must be a member, and so must the person at the top of its chain, so an agent stops where its
	person was taken out and works again when they are added back, with nothing re-issued.
	"""

	if not user.is_active or user.deleted_at is not None or user.id in leaving:
		return INACTIVE

	walked: list[subroutine.db.models.identity.User]

	try:
		walked = chain(session, user)

	except subroutine.errors.ValidationError:
		return UNANSWERED

	if not all(
		entry.is_active and entry.deleted_at is None and entry.id not in leaving
		for entry in walked
	):
		return UNANSWERED

	if workspace_id is None:
		return None

	member = subroutine.db.models.identity.WorkspaceMember
	wanted = {user.id, walked[-1].id}
	seated = set(
		session.scalars(
			sqlalchemy.select(member.user_id).where(
				member.workspace_id == workspace_id, member.user_id.in_(wanted)
			)
		)
	)

	return None if wanted <= seated else OUTSIDE


def can_act (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	*,
	leaving: typing.Collection[uuid.UUID] = (),
	workspace_id: uuid.UUID | None = None,
) -> bool:
	"""Report whether this account can act - :func:`standing`, as a yes or a no (`#1453`)."""

	return standing(session, user, leaving=leaving, workspace_id=workspace_id) is None


def live (
	model: type[subroutine.db.models.identity.User] = subroutine.db.models.identity.User,
) -> sqlalchemy.ColumnElement[bool]:
	"""Return :func:`standing`'s rule for one account as SQL, for a query that selects accounts.

	**The whole of it for a person**, who answers for themselves; an agent's chain cannot be put in
	a ``WHERE``, so a query that may select agents asks :func:`can_act` of each row it keeps.
	"""

	return sqlalchemy.and_(model.is_active.is_(True), model.deleted_at.is_(None))


def answers_for (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> subroutine.db.models.identity.User:
	"""Return the person accountable for ``user``, which is ``user`` itself for a person."""

	return chain(session, user)[-1]


def answers_to (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	account: subroutine.db.models.identity.User,
) -> bool:
	"""Report whether ``user`` answers to ``account``, directly or through other agents - `#4566`.

	**Strictly above it**: an account does not answer to itself. Decision `#4516` is that whoever an
	account answers to may stop it and stands in for it as owner, so this is the one predicate both
	of those ask; :func:`answering_to` is its form for a query. A chain that cannot be walked answers
	to nobody, which refuses rather than grants.
	"""

	if user.id == account.id or not user.is_service_account:
		return False

	try:
		walked = chain(session, user)

	except subroutine.errors.ValidationError:
		return False

	return any(entry.id == account.id for entry in walked[1:])


def answering_to (
	session: sqlalchemy.orm.Session, account: subroutine.db.models.identity.User
) -> set[uuid.UUID]:
	"""Return the ids of every account that answers to ``account`` - :func:`answers_to` for a query.

	For a listing narrowed to what somebody may act on - their agents' credentials and calendar
	feeds (`#4566`) - as ``model.user_id.in_(...)``. Walked by level, as
	:func:`agents_answering_to` is.
	"""

	return {one.id for one in agents_answering_to(session, account)}


def refuse_a_person_act (
	session: sqlalchemy.orm.Session,
	act: str,
	*,
	by: subroutine.db.models.identity.User | None,
	on: subroutine.db.models.identity.User | None = None,
) -> None:
	"""Refuse an agent one of :data:`subroutine.permissions.PERSON_ACTS` - `SR#4565`, decision `#4515`.

	**The one place an account being an agent decides what it may do.** Called first, before any
	role, any superuser bypass and any other check, so a superuser agent is refused as any agent is
	and every refusal is the same sentence with the same status, 403. It lived in seven inline checks
	with two statuses and two orders, and an administering agent issued a working credential for
	another person's agent, signed a person out everywhere, and made itself owner where no owner
	could act, because the agent check ran after the superuser bypass (S6 and S7 of the cold review
	of 2026-10-05).

	``by`` is whoever would take the act - the actor, or for a browser session the account that
	would hold it - and ``None`` an internal caller, which nothing here narrows. ``on`` is the
	account acted on, for the acts an agent may take on itself and the agents that answer to it
	(:data:`subroutine.permissions.WITHIN_ITS_OWN_AGENTS`) and those that are a person's only when
	they name a person (:data:`subroutine.permissions.NAMING_A_PERSON`).
	"""

	if by is None or not by.is_service_account:
		return

	if (
		act in subroutine.permissions.WITHIN_ITS_OWN_AGENTS
		and on is not None
		and (on.id == by.id or answers_to(session, on, by))
	):
		return

	if act in subroutine.permissions.NAMING_A_PERSON and on is not None and on.is_service_account:
		return

	if act == subroutine.permissions.HOLDING_A_BROWSER_SESSION:
		hint = (
			"An agent works through a credential, which carries a scope and a reach a session does "
			f"not: 'subroutine token create --service-account {by.username}'."
		)

	else:
		try:
			person = answers_for(session, by).username

		except subroutine.errors.ValidationError:
			person = None

		hint = (
			"Ask the person it answers to."
			if person is None
			else f"Ask {person!r}, who answers for {by.username!r}."
		)

	unless = (
		f" unless that account answers to it, as {on.username!r} does not"
		if act in subroutine.permissions.WITHIN_ITS_OWN_AGENTS and on is not None
		else ""
	)

	raise subroutine.errors.Forbidden(
		f"{by.username!r} is an agent, and {subroutine.permissions.PERSON_ACTS[act]} is a person's "
		f"act{unless}.",
		hint=hint,
	)


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


def levels_below (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> int:
	"""Return how many levels of agents answer to ``user`` through one another — `#3939`.

	The distance to the furthest of them, which is where a chain moved above ``user`` is longest:
	a transfer measured at the moved agent alone left an agent eight levels below it answering
	through a chain nothing would walk. Walked by level for :func:`agents_answering_to`'s reason,
	and bounded by :data:`MAX_DEPTH`, since anything deeper is broken already.
	"""

	model = subroutine.db.models.identity.User
	seen: set[uuid.UUID] = {user.id}
	frontier = [user.id]
	levels = 0

	for _step in range(MAX_DEPTH):
		fresh = [
			one
			for one in session.scalars(
				sqlalchemy.select(model.id).where(
					model.responsible_user_id.in_(frontier),
					model.is_service_account.is_(True),
					model.deleted_at.is_(None),
				)
			)
			if one not in seen
		]

		if not fresh:
			break

		seen.update(fresh)
		frontier = fresh
		levels += 1

	return levels


def answerable_for_many (
	session: sqlalchemy.orm.Session, user_ids: typing.Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
	"""Return, for each id, the username of the person who answers for it (`#1414`).

	:func:`answers_for` one row at a time is an N+1 the moment a page renders it, and a page is
	exactly where this is wanted: every row naming an agent has to say who is accountable for
	it. So this walks **by level rather than by row** — one query per step of the chain, not one
	per user — which makes the cost a fact about how deeply agents are nested and not about how
	many rows are on the page.

	**In practice that is one query.** An agent answerable to a person resolves in a single
	step, and the person is usually already among the ids asked for, since they hold work on the
	same page. :data:`MAX_DEPTH` bounds the loop for :func:`chain`'s reason.

	A person answers for themselves, so their own username comes back. **An agent whose chain
	does not reach a person is absent from the result** rather than raising: this renders a
	listing, and refusing to draw a page because one account is misconfigured would take the
	whole view away to report something only an administrator can fix. :func:`chain` still
	refuses it at the point where it matters, which is authentication.
	"""

	model = subroutine.db.models.identity.User
	wanted = {identifier for identifier in user_ids if identifier is not None}

	if not wanted:
		return {}

	# Every account met on the way, so a second level costs nothing for somebody already loaded
	# and a cycle cannot be walked twice.
	known: dict[uuid.UUID, tuple[str, bool, uuid.UUID | None]] = {}
	frontier = set(wanted)

	for _step in range(MAX_DEPTH):
		fresh = frontier - set(known)

		if not fresh:
			break

		rows = session.execute(
			sqlalchemy.select(
				model.id, model.username, model.is_service_account, model.responsible_user_id
			).where(model.id.in_(fresh))
		).all()

		for identifier, username, is_agent, responsible in rows:
			known[identifier] = (username, is_agent, responsible)

		frontier = {
			responsible
			for _username, is_agent, responsible in (known[found] for found in fresh if found in known)
			if is_agent and responsible is not None
		}

	answers: dict[uuid.UUID, str] = {}

	for identifier in wanted:
		walked: set[uuid.UUID] = set()
		current = identifier

		while current in known and current not in walked:
			username, is_agent, responsible = known[current]
			walked.add(current)

			if not is_agent:
				answers[identifier] = username
				break

			if responsible is None:
				break

			current = responsible

	return answers


def answerable_name (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> str | None:
	"""Return the name of the person answerable for one account, or ``None`` (`#1420`).

	**The single-account form of :func:`answerable_for_many`, and it is a call to that** rather
	than a second walk: a rule with two implementations is this codebase's most expensive
	recurring defect, and the difference between one account and a page is a list literal.

	For a renderer, so it says ``None`` where :func:`answers_for` raises. A listing that refused
	to draw because one account is misconfigured would take the page away to report something
	only an administrator can fix; :func:`chain` still refuses it where it matters, which is
	authentication.
	"""

	return answerable_for_many(session, [user.id]).get(user.id)


def account_parents_for_many (
	session: sqlalchemy.orm.Session,
	users: typing.Iterable[subroutine.db.models.identity.User],
) -> dict[uuid.UUID, str]:
	"""Return, for each agent among ``users``, the username of its account parent (`#2789`).

	**The first link of the chain, where :func:`answerable_for_many` returns the last.** An
	account parent is the account an agent was created by - :func:`inherited`'s *who handed
	this down* - and it is whom a question goes to when nobody assigned the work (decision
	`#2700` §3). For an agent answerable to a person the two are one name; for a sub-agent
	they are not, and that is the case this exists for.

	A person has none whatever ``responsible_user_id`` holds, because :func:`chain` reads that
	column only for an agent. One query for a page, and none when nothing on it is an agent.
	"""

	linked = {
		user.id: user.responsible_user_id
		for user in users
		if user.is_service_account and user.responsible_user_id is not None
	}

	if not linked:
		return {}

	model = subroutine.db.models.identity.User
	names = dict(
		session.execute(
			sqlalchemy.select(model.id, model.username).where(model.id.in_(set(linked.values())))
		).all()
	)

	return {account: names[parent] for account, parent in linked.items() if parent in names}


def account_parent_name (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> str | None:
	"""Return the username of one account's account parent, or ``None`` (`#2789`).

	The single-account form of :func:`account_parents_for_many`, and a call to it, for
	:func:`answerable_name`'s reason.
	"""

	return account_parents_for_many(session, [user]).get(user.id)


def inherited (actor: subroutine.db.models.identity.User) -> uuid.UUID:
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
	keeps finding rather than a belt beside a brace. The rule a permission cannot express - that an
	*agent* may not choose, however privileged its credential - is a person's act, refused before
	anything else by :func:`refuse_a_person_act` (`SR#4565`, decision `#4515`).
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

	named = session.get(subroutine.db.models.identity.User, wanted)

	if named is None:
		raise subroutine.errors.ValidationError(
			"The account named as answerable for this agent does not exist."
		)

	# **Somebody who can act** (`#4545`, G8 of the cold review of 2026-10-05): an agent made to
	# answer to a deactivated account, or to an agent nobody active answers for, could never act.
	why = standing(session, named)

	if why is not None:
		cannot = "is deactivated" if why == INACTIVE else "answers to nobody who can act"

		raise subroutine.errors.ValidationError(
			f"'{named.username}' {cannot}, so an agent answering to it could never act.",
			hint="Make it answer to somebody who can act.",
		)

	# Walking from the *named* account proves the new agent's chain before it is written: if the
	# person named is themselves an agent, their chain has to reach somebody, and this is the
	# only moment where refusing costs nothing.
	above = chain(session, named)

	# **And measures it** (`#3939`): the new agent is one more link, so under a chain already as
	# long as :data:`MAX_DEPTH` allows, an agent was made that every one of its requests refused.
	if len(above) >= MAX_DEPTH:
		raise subroutine.errors.ValidationError(
			f"An agent answering to '{named.username}' would have a chain of responsibility "
			f"{len(above) + 1} accounts long, and more than {MAX_DEPTH} is treated as broken, so it "
			f"could never act.",
			hint="Make it answer to an account nearer a person.",
		)

	return wanted
