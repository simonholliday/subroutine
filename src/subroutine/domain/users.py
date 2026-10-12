"""Creating people and machine identities.

A service account exists so that an agent's work is attributable to something other than the
human who happens to own its token (docs/design.md §5.2). An agent that cannot be named cannot
be audited, which is why they are users here rather than a flag on a token.

**Nobody here has a password** (`#2923`, Simon's decisions of 2026-09-20 and 2026-10-11). A
person signs in to a browser with a sign-in link and everything else presents a token, so the
hash ``init`` could be given to store was read by nothing, and it went with the flag that took it.
"""

import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.dates
import subroutine.domain.repair
import subroutine.domain.stewardship
import subroutine.domain.text
import subroutine.errors
import subroutine.permissions

#: Column widths from docs/design.md §10.6, enforced here so the refusal names the field rather
#: than arriving as a driver error on PostgreSQL and not at all on SQLite.
MAX_USERNAME_LENGTH = 64

#: Words the search grammar reads as something other than a name - `#3155`. ``assignee:unset``
#: is *assigned to nobody* and ``assignee:set`` *to anybody* (``filtering.UNSET`` and
#: ``filtering.SET``), and ``assignee:me`` is whoever is asking (``selection.CALLER``). An account
#: called one of them could not be named by it: the board drew a person called *unset* under
#: **Nobody**, and a view saved over their chip read back as unassigned work. **Written out here
#: because this module cannot import either of those**, which both import it, and held to them
#: by ``tests/test_services.py``.
READ_AS_SOMETHING_ELSE = frozenset({"set", "unset", "me"})
MAX_EMAIL_LENGTH = 320
MAX_DISPLAY_NAME_LENGTH = 255


def create (
	session: sqlalchemy.orm.Session,
	*,
	username: str,
	email: str | None = None,
	display_name: str | None = None,
	is_service_account: bool = False,
	is_superuser: bool = False,
	timezone: str | None = None,
	responsible_user_id: uuid.UUID | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.identity.User:
	"""Create a person or a machine identity.

	``responsible_user_id`` names who answers for an agent (decision `#473`). Omitted, it is
	*inherited* from whoever is creating it rather than defaulted to nobody: the creator becomes
	the link, so an agent that spawns a sub-agent is what that sub-agent answers to, and walking
	on from there reaches a person. Naming somebody else is a person's act; see
	:mod:`subroutine.domain.accountability` for why an agent may never do it.
	"""

	# **A person's acts, asked first** (`SR#4565`, decision `#4515`), whatever the agent holds: making
	# an administrator of the installation (`#701`), which an administering agent could otherwise do
	# again and again with no person agreeing; creating a person, which `#701` allowed and the
	# decision does not; and choosing who answers for an agent, where the creator is the link and an
	# agent naming anybody else would launder the chain (decision `#473`).
	creator = None if actor is None else actor.user

	if is_superuser:
		subroutine.domain.accountability.refuse_a_person_act(
			session, subroutine.permissions.MAKING_A_SUPERUSER, by=creator
		)

	if not is_service_account:
		subroutine.domain.accountability.refuse_a_person_act(
			session, subroutine.permissions.CREATING_A_PERSON, by=creator
		)

	elif (
		creator is not None
		and responsible_user_id is not None
		and responsible_user_id != subroutine.domain.accountability.inherited(creator)
	):
		subroutine.domain.accountability.refuse_a_person_act(
			session, subroutine.permissions.NAMING_AN_ANSWERER, by=creator
		)

	# The instance tier (docs/design.md §7.1). This act happens outside every workspace, so it is
	# checked against the installation rather than against one — and `authorize_instance`
	# honours a token's scopes even for a superuser, which is what makes it safe to hand an
	# agent a token that may do this and nothing else.
	if actor is not None:
		subroutine.domain.authorization.authorize_instance(
			actor, subroutine.permissions.INSTANCE_USER_CREATE
		)

	name = subroutine.domain.text.fit(
		subroutine.domain.text.require(username, field="username"),
		field="username",
		limit=MAX_USERNAME_LENGTH,
	)
	# **Measured as it is compared, too** (`#4027`): ``username_normalized`` is as wide as the
	# name, and folding can lengthen it.
	subroutine.domain.text.fits_folded(
		name, normalize(name), field="username", limit=MAX_USERNAME_LENGTH
	)

	# **A username is an address segment** (`#3147`): every route about somebody puts it in a
	# path, and ``..`` there is the level above - `subroutine user remove ..` over HTTP removed
	# the workspace. The HTTP client refuses one before sending, so none may be stored.
	subroutine.domain.text.refuse_a_dot_segment(name, field="username", what="a username")

	if name.lower() in READ_AS_SOMETHING_ELSE:
		raise subroutine.errors.ValidationError(
			f"{name!r} cannot be used as a username.",
			errors=[
				subroutine.errors.FieldError(
					field="username",
					code="invalid_field_value",
					message=f"A search reads {name.lower()!r} as a state rather than as a person, "
					"so nobody could be found by it.",
					hint="Choose a name with more to it - a first name is the usual one.",
				)
			],
		)

	if email is not None:
		email = subroutine.domain.text.fit(
			email, field="email", limit=MAX_EMAIL_LENGTH, label="email address"
		)
		# **Measured as it is compared, too** (`#4278`, L-AA-1 of the cold review of 2026-10-03), as the
		# username is (`#4027`): folding can lengthen it, and ``email_normalized`` is as wide as the
		# address - a 500 on PostgreSQL, and an over-long row on SQLite.
		subroutine.domain.text.fits_folded(
			email, normalize(email), field="email", limit=MAX_EMAIL_LENGTH, label="email address"
		)

	if display_name is not None:
		display_name = subroutine.domain.text.fit(
			display_name, field="display_name", limit=MAX_DISPLAY_NAME_LENGTH, label="display name"
		)

	_refuse_duplicate(session, "username", subroutine.db.models.identity.User.username_normalized, normalize(name))

	normalized_email = None if email is None else normalize(email)

	if normalized_email is not None:
		_refuse_duplicate(
			session, "email", subroutine.db.models.identity.User.email_normalized, normalized_email
		)

	# Settled before the row is built, so an unaccountable agent is refused rather than written
	# and corrected. `actor is None` is bootstrap creating the first person, who answers for
	# themselves; the rule refuses that combination for an *agent* rather than inventing one.
	answerable = subroutine.domain.accountability.refuse_an_unaccountable_agent(
		session,
		actor=None if actor is None else actor.user,
		is_service_account=is_service_account,
		responsible_user_id=responsible_user_id,
	)

	# **Checked as `set_timezone` checks it** (`#3934`): an account made with a zone nothing knows
	# was accepted, and its agenda then answered 422 about a zone its owner could not have set.
	if timezone is not None:
		subroutine.domain.dates.zone(timezone)

	user = subroutine.db.models.identity.User(
		username=name,
		username_normalized=normalize(name),
		email=email,
		email_normalized=normalized_email,
		display_name=display_name,
		is_service_account=is_service_account,
		is_superuser=is_superuser,
		responsible_user_id=answerable,
		timezone=timezone,
	)
	session.add(user)
	session.flush()

	# Users are not workspace-scoped, but events are. A user created outside any workspace
	# has no feed to appear in, so the event is written when they are given a membership
	# (see `workspaces.add_member`) rather than invented against a workspace they are not
	# yet in.

	return user


def set_active (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	*,
	active: bool,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> list[subroutine.db.models.identity.User]:
	"""Mark somebody as having left, or bring them back. Returns the agents this affects.

	**`is_active` was enforced and unsettable until now** (`#475`): four code paths refused an
	inactive account and nothing could produce one, so "this person has left" was a state the
	product could not reach. Decision `#473` rests on it — when a person goes, the agents
	answerable to them stop — so it had to become an act somebody can perform.

	**Reactivation is the same operation.** A separate command would be a second copy of the
	last-administrator rule and the same accounting in reverse, and the two would eventually
	disagree about what counts as an administrator.

	The affected agents are returned rather than merely counted, so a caller can name them
	before doing it — `project rename`'s precedent. A deactivation that silently stops a shared
	agent is how a control like this comes to be worked around.

	**An agent changes the standing only of its own agents** (`SR#4565`, decision `#4515`): it
	checked only ``instance:user_create`` until 2026-08-04 (`#487`), so an agent holding that could
	mark a person as having left - and under `#473` every agent answers to a person, so it could
	stop itself and its siblings in a single call nothing undoes. That is a person's act now, asked
	first, and retiring a sub-agent that answers to it is not.

	**Whoever an account answers to may mark it as having left** (`SR#4566`, decision `#4516`), with
	a credential nothing narrows, since it stops every credential the account holds. **Bringing one
	back is starting it**, and stays with ``instance:user_create``: a person who stops their own
	agent and does not administer the installation asks somebody who does to start it again. Doing
	either to your own account is the instance tier's too.
	"""

	subroutine.domain.accountability.refuse_a_person_act(
		session,
		subroutine.permissions.CHANGING_STANDING,
		by=None if actor is None else actor.user,
		on=user,
	)
	subroutine.domain.authentication.refuse_a_read_only_session(actor)

	if actor is not None:
		subroutine.domain.stewardship.refuse_acting_on_an_account(
			session, actor, user, stopping=not active, its_own=False
		)

		if not subroutine.domain.authorization.may_instance(
			actor, subroutine.permissions.INSTANCE_USER_CREATE
		):
			subroutine.domain.authentication.refuse_undominated(
				session,
				actor,
				subroutine.domain.authentication.Bounds(),
				counting_expiry=False,
				act="mark an account as having left",
				hint="Marking an account as having left stops every credential it holds, so only a "
				"credential with no narrowing does it. Use an unrestricted credential, or the "
				"browser.",
			)

	if not active:
		_refuse_deactivating_the_last_administrator(session, user)

	stopping = subroutine.domain.accountability.agents_answering_to(session, user)

	# Written only when it changes, so that re-running this does not move `updated_at` and
	# make a no-op look like an act. `VersionMixin` is a plain column here — nothing
	# increments it for us — so §8.9 would compare a number that never moved otherwise.
	if user.is_active != active:
		user.is_active = active
		user.version += 1
		session.flush()

	return stopping


def set_timezone (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	*,
	timezone: str | None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> None:
	"""Record where somebody keeps their diary — their own account, and nobody else's.

	**§6.5's chain has a user level and nothing could fill it** (`#994`). ``POST /v1/users``
	accepted a zone at creation and no surface could change one afterwards, so "the reader's
	own timezone" resolved to a *workspace's* — which is a server's locality rather than a
	person's. Decision `#989` makes that value decide which bucket of the agenda a task falls
	into on every surface, so it had to become something a person can set.

	**Self only, and Simon's decision of 2026-08-18 is stronger than what was proposed.**

		*"A person should be able to set and update their own timezone. At this moment, I
		don't think we have the need for someone to set anyone else's. A user knows which
		timezone they are in better than anyone else."*

	The proposal was self freely and others with ``instance:user_create``, on the grounds that
	it matches every other cross-account write here. **The reasoning against it is better than
	the symmetry argument**: this level exists to record where *that person* keeps their diary,
	and nobody else can be authoritative about that. A permission to write it would be a
	permission to be wrong on somebody's behalf.

	So there is **no new permission verb**, which is worth saying because the obvious
	implementation invents one: the check is *are you this person*, not something anybody can
	be granted. ``permissions.WORKSPACE_LEVEL`` and the instance verbs are unchanged.

	**An agent is a user too** (`#473`), so it sets its own and nobody sets it for it — which
	is the right answer by accident of the same rule. An agent has no locality, leaves this
	null, and the chain falls through to the workspace exactly as it does today.

	**Creation is deliberately untouched.** An operator bootstrapping an account may still pass
	one; removing an accepted request field is a breaking wire change, it harms nothing, and
	the person can now correct it — which is precisely what they could not do before.

	**Worth Simon's eye and deliberately not decided here**: this is the first write reachable
	by a credential carrying no write scope, because there is no verb to check and identity is
	the whole rule. The harm is bounded — a leaked read-only token of *your own* could shift
	*your own* agenda by some hours, reversibly, visibly and attributed — and an agent's
	credential is its own account, so nothing reaches a person's row by this route. It is
	recorded rather than acted on because inventing a verb is exactly what the decision above
	rules out.
	"""

	subroutine.domain.authentication.refuse_a_read_only_session(actor)

	if actor is not None and actor.user.id != user.id:
		raise subroutine.errors.Forbidden(
			f"You can set your own timezone, not {user.username}'s.",
			hint=(
				"There is no permission that grants this. Ask them to set it - they know "
				"which zone they are in better than anybody else does."
			),
		)

	# Refused here rather than stored and met later, which is `workspaces.create`'s recorded
	# lesson: an unknown zone written to a row surfaces as a 422 naming the *request's*
	# timezone on some later date computation, which is a message about the wrong thing
	# arriving long after the mistake.
	if timezone is not None:
		subroutine.domain.dates.zone(timezone)

	# Written only when it changes, so re-running this does not move `updated_at` and make a
	# no-op look like an act. `VersionMixin` is a plain column here — nothing increments it
	# for us — so §8.9 would otherwise compare a number that never moved.
	if user.timezone != timezone:
		user.timezone = timezone
		user.version += 1
		session.flush()


def transfer (
	session: sqlalchemy.orm.Session,
	agent: subroutine.db.models.identity.User,
	*,
	to: subroutine.db.models.identity.User,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> None:
	"""Hand an agent to somebody else, who becomes answerable for what it does — `#478`.

	**The other half of the leaver path.** Agents stop when the person answerable for them goes
	(`#479`), so handing one over is the only way to keep it running — which makes this part of
	somebody leaving rather than a refinement of it. Without it, marking a leaver inactive means
	losing their agents, and a control that costs that much is one people route around.

	**Only a person may hand it over, and it goes to a person or to an agent whose own chain ends
	at one** (`#476`; this said a person alone could take it on until `#3944`, which the code
	never held). Handing over is the same rule as creation: an agent that could move
	accountability could move it *off* itself, which is the laundering
	:mod:`subroutine.domain.accountability` refuses one step earlier. A person doing it is the
	act being modelled — somebody agreeing to answer for a thing — and it is not an act anything
	can perform on their behalf.
	"""

	# **A person's act, asked first** (`SR#4565`, decision `#4515`).
	subroutine.domain.accountability.refuse_a_person_act(
		session,
		subroutine.permissions.HANDING_AN_AGENT_OVER,
		by=None if actor is None else actor.user,
	)

	if actor is not None:
		subroutine.domain.authorization.authorize_instance(
			actor, subroutine.permissions.INSTANCE_USER_CREATE
		)

	if not agent.is_service_account:
		raise subroutine.errors.ValidationError(
			f"{agent.username} is a person, and a person answers for themselves.",
			hint="Only an agent has somebody else accountable for it.",
		)

	# **A cycle is asked for by name** (`#3939`). Handing an agent to something below itself is a
	# loop every foreign key resolves happily, and the move makes one exactly when the target
	# already answers to the agent. It was found by assigning and walking, and every refusal the
	# walk raised was reworded as this one, so a chain that was only too long was called a loop.
	if to.id == agent.id or any(
		below.id == to.id
		for below in subroutine.domain.accountability.agents_answering_to(session, agent)
	):
		raise subroutine.errors.ValidationError(
			f"{to.username} already answers to {agent.username}, directly or through another "
			f"agent, so this would leave neither of them answering to a person.",
			hint="Hand it to somebody outside the chain below it.",
		)

	# **To somebody who can act** (`#4545`, G8 of the cold review of 2026-10-05). Handed to a
	# deactivated person, or to an agent nobody active answers for, it stopped the moment it moved,
	# and nothing said so.
	why = subroutine.domain.accountability.standing(session, to)

	if why is not None:
		cannot = (
			"is deactivated"
			if why == subroutine.domain.accountability.INACTIVE
			else "is an agent that nobody who can act answers for"
		)

		raise subroutine.errors.ValidationError(
			f"{to.username} {cannot}, so {agent.username} would stop working the moment it was "
			f"handed over.",
			hint="Hand it to somebody who can act.",
		)

	# **Measured at the deepest agent below it, where the chain is longest** (`#3939`). Walking the
	# moved agent alone let one with eight levels below it go to an agent ten deep, and its deepest
	# sub-agent could not act, with nothing said. A target whose own chain reaches no person is
	# refused in the chain's own words, and all of it before anything moves.
	depth = subroutine.domain.accountability.MAX_DEPTH
	below = subroutine.domain.accountability.levels_below(session, agent)
	longest = below + 1 + len(subroutine.domain.accountability.chain(session, to))

	if longest > depth:
		whose = "it" if below == 0 else "an agent below it"

		raise subroutine.errors.ValidationError(
			f"Handing {agent.username} to {to.username} would give {whose} a chain of "
			f"responsibility {longest} accounts long, and more than {depth} is treated as broken, "
			f"so it could not act.",
			hint="Hand it to somebody nearer a person.",
		)

	agent.responsible_user_id = to.id
	agent.version += 1
	session.flush()


def _refuse_deactivating_the_last_administrator (
	session: sqlalchemy.orm.Session, going: subroutine.db.models.identity.User
) -> None:
	"""Refuse a deactivation that would leave the instance with nobody able to administer it.

	The same argument as ``workspaces._refuse_leaving_nobody_who_can_administer`` one tier up: an
	instance with no active superuser cannot be repaired from inside, and under decision `#473` it
	would stop every agent as well — because every chain terminates at a person, and an inactive
	person ends it. §12.4's direct-database recovery exists so that is survivable, not so that it
	is the plan.

	**Counted by the one predicate for every last one** (`SR#4569`, decision `#4526`): superusers
	who are people and can act. **Refused here, where every other scope is named instead** (Simon,
	2026-10-09): deactivating is the only way an installation loses a superuser, since none can be
	demoted, and local mode's recovery relies on one existing (decision `#4514`).
	"""

	if going.id not in subroutine.domain.repair.repairers(session, subroutine.domain.repair.INSTALLATION):
		return

	model = subroutine.db.models.identity.User

	# **Every active superuser is locked before the others are counted** (`#1178`), for the
	# reason the workspace's own rule gives: two deactivations at once each counted the other
	# as staying. The second waits for the first, then counts what it left. **By a write that
	# changes nothing**, for that rule's reasons too (`#3899`): SQLite takes no lock for a read,
	# and ``FOR UPDATE`` waited on a foreign key's check where this lock does not.
	session.execute(
		sqlalchemy.update(model)
		.where(model.is_superuser.is_(True), subroutine.domain.accountability.live(model))
		.values(updated_at=model.updated_at)
		.execution_options(synchronize_session=False)
	)

	if subroutine.domain.repair.repairers(
		session, subroutine.domain.repair.INSTALLATION, leaving=(going.id,)
	):
		return

	raise subroutine.errors.ValidationError(
		f"{going.username} is the only person who can administer this instance.",
		hint=(
			"Make somebody else a superuser first. An instance with nobody able to administer "
			"it cannot be repaired from inside, and every agent here answers to a person who "
			"is still here."
		),
	)


def within_reach (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	model: type[subroutine.db.models.identity.User] = subroutine.db.models.identity.User,
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate saying an account is one this principal may read - `SR#4945`.

	**An account is read within reach** (decision `#4943`, Simon 2026-10-11): by whoever shares a
	workspace with it, by its own chain - itself, whoever it answers to and whatever answers to
	it - and by an administrator of accounts, who holds ``instance:user_create``. Every other
	kind of row had a reach rule and this one answered every account on the installation to any
	credential, however narrowed, with who administers the installation, who is active, who
	answers to whom and where each of them is.

	**Sharing a workspace is asked of the one predicate that says which workspaces a credential
	reaches** (:func:`subroutine.domain.authorization.reaches`), so one pinned to a workspace reads
	the accounts of that one, one narrowed to some projects those of the workspaces holding them,
	and an agent those of the workspaces it works in. An administrator's credential that is pinned
	or narrowed holds nothing over the installation, and reads as anybody's does.

	**Naming an account to act on it is not a read of it**, as naming a project to file into is
	not a read of the project: :func:`by_username` still resolves any name, since a username is an
	identifier somebody types and cannot be a secret (decision `#161`). ``None`` is an internal
	caller, which nothing here narrows.
	"""

	if actor is None or subroutine.domain.authorization.may_instance(
		actor, subroutine.permissions.INSTANCE_USER_CREATE, reading=True
	):
		return sqlalchemy.true()

	try:
		above = [entry.id for entry in subroutine.domain.accountability.chain(session, actor.user)]

	except subroutine.errors.ValidationError:
		above = [actor.user.id]

	member = sqlalchemy.orm.aliased(subroutine.db.models.identity.WorkspaceMember)

	return sqlalchemy.or_(
		model.id.in_(above),
		model.id.in_(subroutine.domain.accountability.sees_as(actor.user.id)),
		model.id.in_(
			sqlalchemy.select(member.user_id).where(
				subroutine.domain.authorization.reaches(session, actor, member.workspace_id)
			)
		),
	)


def readable (
	session: sqlalchemy.orm.Session,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
	answers_to: str | None = None,
) -> sqlalchemy.Select[subroutine.db.models.identity.User]:
	"""Return the statement behind the account directory, narrowed by what was asked.

	**A statement rather than rows, which is what lets both transports page it identically**
	(`SR#2384`). This was ``listed``, which took a ``limit`` of 200, applied its own ordering and
	handed back a list — so ``GET /v1/users`` could not say how much of the answer it was
	returning and said ``has_more: false`` regardless. ``readable_projects`` is the shape this
	follows: the domain decides which rows there are, and each caller decides how many of them
	to take and in what order.

	**The accounts within the asker's reach** (`SR#4945`, decision `#4943`), which
	:func:`within_reach` says. It was every account, to anybody authenticated, by a decision
	recorded here: a name you cannot look up is one you have to be told out of band. That still
	holds where it mattered - the people you share a workspace with are all listed - and a name
	from anywhere else can still be typed, since naming an account is not reading it. This view
	carries no email address and no content at all, as before.

	**``answers_to`` asks the question rather than deriving it** (`SR#2387`). It names a person
	and returns the live service accounts answerable to them, directly or through another, which
	is exactly what :func:`subroutine.domain.accountability.agents_answering_to` computes. It is
	not a column and cannot be one: the chain is a bounded walk outward, so there is no value for
	SQL to compare — the same reason ``priority_score`` is orderable and not filterable. A
	username that names nobody the asker can read is refused by name rather than answered with an
	empty page.

	Ordering is :data:`subroutine.domain.ordering.DEFAULT_USER_ORDER`'s and is applied by the
	caller, so a cursor and a plain read cannot disagree about where a page stopped.
	"""

	model = subroutine.db.models.identity.User
	statement = sqlalchemy.select(model).where(
		model.deleted_at.is_(None), within_reach(session, actor, model)
	)

	if answers_to is None:
		return statement

	# Resolved through `read`, so an unknown name is the refusal that function already writes
	# rather than a page with nothing on it — `#264`'s rule that a refusal names what it looked
	# at, applied to a filter.
	answering = subroutine.domain.accountability.agents_answering_to(
		session, read(session, answers_to, actor=actor)
	)

	return statement.where(model.id.in_([one.id for one in answering]))


def read (
	session: sqlalchemy.orm.Session,
	username: str,
	*,
	actor: subroutine.domain.authentication.Principal | None,
) -> subroutine.db.models.identity.User:
	"""Return the account with this username for somebody to read, or refuse as :func:`by_username` does.

	**One within the asker's reach** (`SR#4945`, decision `#4943`): an account outside it is
	answered exactly as a name nobody has, so the read says nothing of an account the listing
	leaves out. An act that names an account - adding somebody to a workspace, sharing a project
	with them - asks :func:`by_username`, which this is with the reach in front.
	"""

	account = by_username(session, username)
	model = subroutine.db.models.identity.User
	seen = session.scalar(
		sqlalchemy.select(model.id).where(model.id == account.id, within_reach(session, actor, model))
	)

	if seen is None:
		raise _nobody_called(username)

	return account


def by_username (
	session: sqlalchemy.orm.Session, username: str
) -> subroutine.db.models.identity.User:
	"""Return the live account with this username, or refuse by name.

	Case-insensitive through the normalised column, because a username is an address somebody
	types and ``Simon`` and ``simon`` being two accounts would be a trap rather than a feature —
	which is what the unique index on that column already says.
	"""

	# **Half a character is refused by name** (`#4287`, M4 of the cold review of 2026-10-03):
	# the driver cannot encode a lone surrogate, so it reached the database as a 500 and the
	# terminal as a traceback.
	subroutine.domain.text.readable(username, field="username")

	model = subroutine.db.models.identity.User
	found = session.scalars(
		sqlalchemy.select(model).where(
			model.username_normalized == normalize(username), model.deleted_at.is_(None)
		)
	).first()

	if found is None:
		raise _nobody_called(username)

	return found


def _nobody_called (username: str) -> subroutine.errors.NotFound:
	"""Return the refusal for a name that is nobody's, or nobody's the asker can read.

	**One sentence for both** (`SR#4945`), so a read cannot be used to tell an account out of
	reach from one that does not exist.
	"""

	return subroutine.errors.NotFound(
		f"There is no account called {username!r} here.",
		hint="Run 'subroutine user list' to see who there is.",
	)


def member (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, given: str, *, field: str
) -> subroutine.db.models.identity.User:
	"""Return the member of this workspace named by a username or an id, or refuse by name.

	**Membership, not existence**, and that is the whole of it. Handing something to somebody
	who is not in the workspace hands it to somebody who cannot see it — so the account being
	real is not the question, and a check that asks only that produces a document owned by a
	stranger and reports success.

	**Here rather than in the caller**, because there were two and they disagreed:
	:func:`subroutine.domain.tasks.assignee_for` asked this properly, ``documents.create``
	asked nothing at all — an unknown id reached the foreign key and left as a 500 — and
	``documents.update`` asked whether the account existed and never whether it was a member.
	Three answers to one question, one of them a crash.

	``field`` names the field in the refusal, because *assignee* and *owner* are what the
	caller sent and neither reader should be told about the other.

	A value that parses as a UUID is taken as an id, matching ``id_or_ref`` and ``id_or_key``
	and :func:`subroutine.domain.selection.user`; anything else is a username.
	"""

	# **Half a character is refused by name** (`#4287`, M4 of the cold review of 2026-10-03):
	# the driver cannot encode a lone surrogate, so it reached the database as a 500 and the
	# terminal as a traceback.
	subroutine.domain.text.readable(given, field=field)

	try:
		identifier = uuid.UUID(given)

	except ValueError:
		return _member_by_name(session, workspace_id, given, field=field)

	model = subroutine.db.models.identity.User
	found = session.scalars(_members(workspace_id).where(model.id == identifier)).one_or_none()

	if found is not None:
		return found

	raise subroutine.errors.ValidationError(
		f"There is nobody with the id {given!r} in this workspace.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="not_found",
				message=f"No member of this workspace has the id {given!r}.",
				hint="Name them by username instead - 'subroutine user list' shows them.",
			)
		],
	)


def _member_by_name (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, username: str, *, field: str
) -> subroutine.db.models.identity.User:
	"""Return a member of this workspace by username, or say who is here."""

	model = subroutine.db.models.identity.User
	found = session.scalars(
		_members(workspace_id).where(model.username_normalized == normalize(username))
	).one_or_none()

	if found is not None:
		return found

	available = sorted(member.username for member in session.scalars(_members(workspace_id)))

	raise subroutine.errors.ValidationError(
		f"There is nobody called {username!r} in this workspace.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="not_found",
				message=f"No member of this workspace is called {username!r}.",
				hint=f"Members here: {', '.join(available)}." if available else None,
			)
		],
	)


def _members (
	workspace_id: uuid.UUID,
) -> sqlalchemy.Select[subroutine.db.models.identity.User]:
	"""Return a select over the live members of one workspace."""

	model = subroutine.db.models.identity.User
	membership = subroutine.db.models.identity.WorkspaceMember

	return (
		sqlalchemy.select(model)
		.join(membership, membership.user_id == model.id)
		.where(membership.workspace_id == workspace_id, model.deleted_at.is_(None))
	)


def normalize (value: str) -> str:
	"""Return the comparison form of a username or email address.

	PostgreSQL has ``citext`` and SQLite does not, so uniqueness is enforced on a stored
	normalised column rather than on a functional index that only one backend can build.
	"""

	return " ".join(value.strip().lower().split())


def _refuse_duplicate (
	session: sqlalchemy.orm.Session,
	field: str,
	column: sqlalchemy.orm.InstrumentedAttribute[str] | sqlalchemy.orm.InstrumentedAttribute[str | None],
	value: str,
) -> None:
	"""Raise if a live user already holds this value.

	The partial unique index is the real guarantee; this exists so the caller gets a named
	field and a sentence rather than an integrity error from the driver.
	"""

	model = subroutine.db.models.identity.User

	existing = session.scalars(
		sqlalchemy.select(model.id).where(column == value, model.deleted_at.is_(None))
	).first()

	if existing is not None:
		raise subroutine.errors.Conflict(
			f"That {field} is already taken.",
			code="duplicate_key",
			errors=[
				subroutine.errors.FieldError(
					field=field, code="duplicate_key", message=f"{value!r} is already in use."
				)
			],
		)
