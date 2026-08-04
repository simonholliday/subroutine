"""Creating people and machine identities, and giving people passwords.

A service account has no password and exists so that an agent's work is attributable to
something other than the human who happens to own its token (SPEC.md §5.2). An agent that
cannot be named cannot be audited, which is why they are users here rather than a flag on
a token.
"""

import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.auth
import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.text
import subroutine.errors
import subroutine.permissions

#: Column widths from SPEC.md §10.6, enforced here so the refusal names the field rather
#: than arriving as a driver error on PostgreSQL and not at all on SQLite.
MAX_USERNAME_LENGTH = 64
MAX_EMAIL_LENGTH = 320
MAX_DISPLAY_NAME_LENGTH = 255


def create (
	session: sqlalchemy.orm.Session,
	*,
	username: str,
	email: str | None = None,
	display_name: str | None = None,
	password: str | None = None,
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

	# The instance tier (SPEC.md §7.1). This act happens outside every workspace, so it is
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

	if email is not None:
		email = subroutine.domain.text.fit(
			email, field="email", limit=MAX_EMAIL_LENGTH, label="email address"
		)

	if display_name is not None:
		display_name = subroutine.domain.text.fit(
			display_name, field="display_name", limit=MAX_DISPLAY_NAME_LENGTH, label="display name"
		)

	if password is not None and is_service_account:
		raise subroutine.errors.ValidationError(
			"A service account has no password — it authenticates with a token.",
			errors=[
				subroutine.errors.FieldError(
					field="password",
					code="invalid_field_value",
					message="Service accounts cannot have a password.",
					hint="Create it without one, then issue a token for it.",
				)
			],
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

	user = subroutine.db.models.identity.User(
		username=name,
		username_normalized=normalize(name),
		email=email,
		email_normalized=normalized_email,
		display_name=display_name,
		password_hash=None if password is None else _hash(password),
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
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize_instance(
			actor, subroutine.permissions.INSTANCE_USER_CREATE
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

	**Only a person may take it on, and only a person may hand it over.** Both halves of that
	are the same rule as creation: an agent that could move accountability could move it *off*
	itself, which is the laundering :mod:`subroutine.domain.accountability` refuses one step
	earlier. A person doing it is the act being modelled — somebody agreeing to answer for a
	thing — and it is not an act anything can perform on their behalf.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize_instance(
			actor, subroutine.permissions.INSTANCE_USER_CREATE
		)

		if actor.user.is_service_account:
			raise subroutine.errors.Forbidden(
				"An agent cannot decide who answers for another agent. Somebody has to agree "
				"to be accountable, and that is a person's act."
			)

	if not agent.is_service_account:
		raise subroutine.errors.ValidationError(
			f"{agent.username} is a person, and a person answers for themselves.",
			hint="Only an agent has somebody else accountable for it.",
		)

	previous = agent.responsible_user_id
	agent.responsible_user_id = to.id

	# Proved against the tree as it will be, not as it was: assigning first and walking after is
	# what catches handing an agent to something below itself, which is a cycle every foreign key
	# resolves happily. Put back on refusal so a refused transfer changes nothing.
	try:
		subroutine.domain.accountability.chain(session, agent)

	except subroutine.errors.ValidationError as looped:
		agent.responsible_user_id = previous

		# The chain's own message names the cycle, which is right where it is raised and wrong
		# here: somebody handing an agent over asked a different question and wants it answered
		# in those terms.
		raise subroutine.errors.ValidationError(
			f"{to.username} already answers to {agent.username}, directly or through another "
			f"agent, so this would leave neither of them answering to a person.",
			hint="Hand it to somebody outside the chain below it.",
		) from looped

	agent.version += 1
	session.flush()


def _refuse_deactivating_the_last_administrator (
	session: sqlalchemy.orm.Session, going: subroutine.db.models.identity.User
) -> None:
	"""Refuse a deactivation that would leave the instance with nobody able to administer it.

	The same argument as ``workspaces._refuse_removing_the_last_administrator`` one tier up: an
	instance with no active superuser cannot be repaired from inside, and under decision
	`#473` it would stop every agent as well — because every chain terminates at a person, and
	an inactive person ends it. §12.4's direct-database recovery exists so that is survivable,
	not so that it is the plan.
	"""

	if not going.is_superuser or not going.is_active:
		return

	model = subroutine.db.models.identity.User
	others = session.scalars(
		sqlalchemy.select(model.id).where(
			model.is_superuser.is_(True),
			model.is_active.is_(True),
			model.is_service_account.is_(False),
			model.deleted_at.is_(None),
			model.id != going.id,
		)
	).first()

	if others is not None:
		return

	raise subroutine.errors.ValidationError(
		f"{going.username} is the only person who can administer this instance.",
		hint=(
			"Make somebody else a superuser first. An instance with nobody able to administer "
			"it cannot be repaired from inside, and every agent here answers to a person who "
			"is still here."
		),
	)


def listed (
	session: sqlalchemy.orm.Session,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
	limit: int = 200,
) -> list[subroutine.db.models.identity.User]:
	"""Return the live accounts on this instance, oldest first — item ``#174``.

	**Readable by anyone who is authenticated, and that is a decision.** Adding a colleague to a
	workspace means naming them, and a name you cannot look up is one you have to be told out of
	band — which for the commonest case, "who is on this instance", turns a one-line command into
	a conversation. Decision ``#161`` is what makes it safe to say: identifiers are unique and
	public, content is neither, and this view carries no email address and no content at all.

	Oldest first, because the first account is the one ``init`` made and the operator reading
	this is usually looking for the ones that came after it.
	"""

	model = subroutine.db.models.identity.User

	return list(
		session.scalars(
			sqlalchemy.select(model)
			.where(model.deleted_at.is_(None))
			.order_by(model.created_at, model.username)
			.limit(limit)
		)
	)


def by_username (
	session: sqlalchemy.orm.Session, username: str
) -> subroutine.db.models.identity.User:
	"""Return the live account with this username, or refuse by name.

	Case-insensitive through the normalised column, because a username is an address somebody
	types and ``Simon`` and ``simon`` being two accounts would be a trap rather than a feature —
	which is what the unique index on that column already says.
	"""

	model = subroutine.db.models.identity.User
	found = session.scalars(
		sqlalchemy.select(model).where(
			model.username_normalized == normalize(username), model.deleted_at.is_(None)
		)
	).first()

	if found is None:
		raise subroutine.errors.NotFound(
			f"There is no account called {username!r} here.",
			hint="Run 'subroutine user list' to see who there is.",
		)

	return found


def set_password (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	password: str,
) -> None:
	"""Replace a user's password, refusing one that is too weak to be worth storing."""

	if user.is_service_account:
		raise subroutine.errors.ValidationError(
			"A service account has no password — it authenticates with a token.",
			errors=[
				subroutine.errors.FieldError(
					field="password",
					code="invalid_field_value",
					message="Service accounts cannot have a password.",
				)
			],
		)

	user.password_hash = _hash(password)
	session.flush()


def verify_password (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	password: str,
) -> bool:
	"""Check a password, upgrading its stored hash if the parameters have moved on.

	The rehash happens here because this is the only moment the plaintext exists. Skip it
	and an installation's oldest accounts keep their weakest hashes for as long as they
	live (SPEC.md §7.6).
	"""

	if user.password_hash is None:
		return False

	if not subroutine.auth.verify_password(user.password_hash, password):
		return False

	if subroutine.auth.password_needs_rehash(user.password_hash):
		user.password_hash = subroutine.auth.hash_password(password)
		session.flush()

	return True


def normalize (value: str) -> str:
	"""Return the comparison form of a username or email address.

	PostgreSQL has ``citext`` and SQLite does not, so uniqueness is enforced on a stored
	normalised column rather than on a functional index that only one backend can build.
	"""

	return " ".join(value.strip().lower().split())


def _hash (password: str) -> str:
	"""Hash a password, refusing one that fails the published rules."""

	problem = subroutine.auth.password_problem(password)

	if problem is not None:
		raise subroutine.errors.ValidationError(
			problem,
			errors=[
				subroutine.errors.FieldError(
					field="password", code="invalid_field_value", message=problem
				)
			],
		)

	return subroutine.auth.hash_password(password)


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
