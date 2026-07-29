"""Creating people and machine identities, and giving people passwords.

A service account has no password and exists so that an agent's work is attributable to
something other than the human who happens to own its token (SPEC.md §5.2). An agent that
cannot be named cannot be audited, which is why they are users here rather than a flag on
a token.
"""

import sqlalchemy
import sqlalchemy.orm

import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
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
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.identity.User:
	"""Create a person or a machine identity."""

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

	user = subroutine.db.models.identity.User(
		username=name,
		username_normalized=normalize(name),
		email=email,
		email_normalized=normalized_email,
		display_name=display_name,
		password_hash=None if password is None else _hash(password),
		is_service_account=is_service_account,
		is_superuser=is_superuser,
		timezone=timezone,
	)
	session.add(user)
	session.flush()

	# Users are not workspace-scoped, but events are. A user created outside any workspace
	# has no feed to appear in, so the event is written when they are given a membership
	# (see `workspaces.add_member`) rather than invented against a workspace they are not
	# yet in.

	return user


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
