"""Who you are when there is no server — docs/design.md §12.1a.

The light commands open the database directly through the service layer, and the service
layer takes a :class:`Principal` and calls ``authorize()``. So local mode has to produce
one, and where it comes from is a decision rather than a detail. Two plausible answers are
both wrong: a flag that skips the permission check means every permission bug in the
personal path stays hidden until the API exists, and a synthetic all-powerful principal is
the same mistake wearing a hat. **The check runs in local mode exactly as it runs over
HTTP.**

**There is no local password prompt, and that is deliberate.** Anyone who can read the
database file can read every row in it with ``sqlite3`` regardless of what this program
asks them for. The filesystem permission *is* the authentication, and §1.4 forbids making
somebody setting up a to-do list meet a token.

Resolution order:

1. ``SUBROUTINE_TOKEN``, if set — resolved through the same :func:`authenticate` the API
   uses, minus the HTTP. **This is what lets an agent be constrained without running a
   server**: hand it a project-scoped token and the CLI refuses out-of-scope work at the
   same place, with the same message. Without it, an agent invoking the CLI locally holds
   unrestricted authority over everything in the database.
2. The only user, if the database holds exactly one. The ordinary personal case, which
   asks nothing of anybody.
3. ``local_user`` from the configuration, naming which one. Absent, the command stops and
   lists the candidates — guessing whose to-do list is on screen is not an error that
   announces itself.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

#: How many usernames a "which of these did you mean" message lists before it gives up and
#: says how many there are. A hundred-user installation is not running in local mode, but
#: printing a hundred names would be a worse failure than the one it is reporting.
MAX_CANDIDATES = 12


def principal (
	session: sqlalchemy.orm.Session,
	*,
	token: str | None = None,
	local_user: str | None = None,
	token_source: str | None = None,
) -> subroutine.domain.authentication.Principal:
	"""Return who this process is acting as, or refuse with what to do about it.

	``token_source`` names where the credential came from, for the refusal to quote. It is
	optional because a caller may genuinely not know — a test, or anything that was handed a
	token rather than resolving one — and a message that says "a credential" is honest where
	one naming the wrong file is not.
	"""

	if token:
		return _from_token(session, token, source=token_source)

	if local_user:
		return _named(session, local_user)

	return _sole(session)


def workspace_for (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
) -> subroutine.db.models.identity.Workspace:
	"""Return the workspace the personal path works in.

	The oldest one this person belongs to, which for anybody who has run ``init`` and
	nothing else is the only one. Personal commands never name a workspace (§1.4), so this
	is where the word stops — and a token pinned to one workspace narrows it here, which is
	the same rule the API applies.
	"""

	workspace = subroutine.db.models.identity.Workspace
	member = subroutine.db.models.identity.WorkspaceMember

	statement = (
		sqlalchemy.select(workspace)
		.join(member, member.workspace_id == workspace.id)
		.where(member.user_id == principal.user.id, workspace.deleted_at.is_(None))
		.order_by(workspace.created_at)
	)

	if principal.pinned_workspace_id is not None:
		statement = statement.where(workspace.id == principal.pinned_workspace_id)

	found = session.scalars(statement).first()

	if found is not None:
		return found

	raise subroutine.errors.NotFound(
		f"{principal.user.username} does not belong to any workspace.",
		hint="Run 'subroutine init' to set one up, or ask an administrator to add you.",
	)


def _from_token (
	session: sqlalchemy.orm.Session, token: str, *, source: str | None = None
) -> subroutine.domain.authentication.Principal:
	"""Resolve a presented token, with the failure said plainly.

	A token that has expired or been revoked is an error, never a quiet fall-through to
	rule 2. A credential that stops narrowing when it lapses is worse than no credential:
	the agent would carry on working, with more authority than it had a moment ago.

	**The door is still ``local``, and a token does not make one** — `SR#1415`, and
	`SR#1426` §2's definition is the test: *there is no request at all*. A credential here
	narrows what the caller may do; it does not change where the caller is, which is a process
	opening the database file directly.

	**This is the branch that would otherwise say nothing**, and it is the commonest agent
	setup rather than an edge: an agent given its own identity on a personal machine holds
	``SUBROUTINE_TOKEN_LOCAL`` and reaches the database through this function. Left unset, every
	event it wrote would record no door while a person at the same terminal recorded ``local``.
	"""

	try:
		return subroutine.domain.authentication.authenticate(
			session, token, interface=subroutine.domain.authentication.LOCAL
		)

	except subroutine.errors.SubroutineError as error:
		# **Named from where it actually came from** (`#175`). This said "SUBROUTINE_TOKEN
		# was set" whatever the source was, and then told the operator to unset a variable
		# that in the common case was never set — while the credential sat in
		# `credentials.toml`, unmentioned. `credentials.resolve` has always known which of the
		# four places won; it simply was not asked.
		where = f"The token from {source}" if source else "The token supplied"

		raise subroutine.errors.Unauthenticated(
			f"{where} could not be used: {error.detail}",
			hint=(
				f"Remove it from {source} to use this database directly, or issue a new one "
				f"with 'subroutine token create'."
				if source
				else "Issue a new one with 'subroutine token create'."
			),
		) from None


def _named (
	session: sqlalchemy.orm.Session, username: str
) -> subroutine.domain.authentication.Principal:
	"""Resolve the account named by ``local_user``.

	**Deactivated accounts are refused**, matching :func:`_sole` and the token path. This
	was the odd one out: it filtered only ``deleted_at``, so somebody could leave, have
	their account deactivated, and a ``local_user`` line left in a configuration file would
	go on working. Three ways to become a principal, and they now agree.
	"""

	model = subroutine.db.models.identity.User
	normalized = subroutine.domain.users.normalize(username)

	found = session.scalars(
		sqlalchemy.select(model).where(
			model.username_normalized == normalized,
			model.deleted_at.is_(None),
			model.is_active.is_(True),
		)
	).one_or_none()

	if found is None:
		raise subroutine.errors.NotFound(
			f"There is no account called {username!r} in this database, or it is no longer "
			"active.",
			hint=_candidates_hint(session),
		)

	return subroutine.domain.authentication.Principal(
		user=found, interface=subroutine.domain.authentication.LOCAL
	)


def _sole (session: sqlalchemy.orm.Session) -> subroutine.domain.authentication.Principal:
	"""Resolve the only account, or explain why there is a choice to be made."""

	users = _live_users(session, limit=2)

	if len(users) == 1:
		return subroutine.domain.authentication.Principal(
			user=users[0], interface=subroutine.domain.authentication.LOCAL
		)

	if not users:
		raise subroutine.errors.NotFound(
			"This database has no accounts in it.",
			hint="Run 'subroutine init' to set one up.",
		)

	raise subroutine.errors.ValidationError(
		"This database has more than one account, so there is no way to tell whose "
		"to-do list to show.",
		code="missing_field",
		hint=_candidates_hint(session),
		errors=[
			subroutine.errors.FieldError(
				field="local_user",
				code="missing_field",
				message="Set 'local_user' to say which account local commands act as.",
			)
		],
	)


def _candidates_hint (session: sqlalchemy.orm.Session) -> str:
	"""Return a hint naming the accounts that exist, without printing a whole directory."""

	users = _live_users(session, limit=MAX_CANDIDATES + 1)
	names = [user.username for user in users[:MAX_CANDIDATES]]
	listed = ", ".join(names)

	if len(users) > MAX_CANDIDATES:
		listed += f", and {_count(session) - MAX_CANDIDATES} more"

	return f"Set 'local_user' in your configuration to one of: {listed}."


def _live_users (
	session: sqlalchemy.orm.Session, *, limit: int
) -> list[subroutine.db.models.identity.User]:
	"""Return the *people* who could own the to-do list on screen, oldest first.

	**Service accounts are excluded, and that is not a detail.** They were counted until
	2026-07-30, which meant ``subroutine token create --service-account claude`` — the command
	§12.3a exists for — immediately broke ``subroutine add`` with "this database has more than
	one account, so there is no way to tell whose to-do list to show". Setting up an agent
	should not cost you your own to-do list, and a machine identity was never a candidate for
	the answer to *whose* list this is.

	Naming one explicitly still works: ``local_user = "claude"`` is a deliberate choice to act
	as the agent, which is how its scoping is checked without running a server (§12.1a). Only
	the *guess* narrows.
	"""

	model = subroutine.db.models.identity.User

	return list(
		session.scalars(
			sqlalchemy.select(model)
			.where(
				model.deleted_at.is_(None),
				model.is_active.is_(True),
				model.is_service_account.is_(False),
			)
			.order_by(model.created_at)
			.limit(limit)
		)
	)


def _count (session: sqlalchemy.orm.Session) -> int:
	"""Return how many accounts there are."""

	model = subroutine.db.models.identity.User

	return session.scalar(
		sqlalchemy.select(sqlalchemy.func.count())
		.select_from(model)
		.where(
			model.deleted_at.is_(None),
			model.is_active.is_(True),
			model.is_service_account.is_(False),
		)
	) or 0


def readable_workspace_ids (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
) -> list[uuid.UUID]:
	"""Return every workspace this principal may read, for a query that spans them.

	The agenda spans all readable workspaces by default (§8.6). A token pinned to one
	narrows this to that one, which is where the pin does its work.

	The query itself lives in :func:`subroutine.domain.workspaces.readable`, because
	``/v1/me`` needs the same set with the rest of each row attached, and two copies of
	"which workspaces can this person reach" is exactly the kind of pair that drifts.
	"""

	return [
		workspace.id for workspace in subroutine.domain.workspaces.readable(session, principal)
	]


def describe (principal: subroutine.domain.authentication.Principal) -> str:
	"""Return a short description of who is acting, for ``doctor`` and ``--verbose``."""

	# Three cases and three answers. Told apart by asking which credential was presented
	# rather than by the absence of a token, which would have reported a signed-in browser
	# as somebody holding the database file (`#248`).
	if principal.token is not None:
		how = "a token"

	elif principal.session is not None:
		how = "a browser session"

	else:
		how = "the local database"

	scopes: typing.Sequence[str] = principal.scopes

	if scopes:
		return f"{principal.user.username}, via {how}, scoped to: {', '.join(sorted(scopes))}"

	return f"{principal.user.username}, via {how}"
