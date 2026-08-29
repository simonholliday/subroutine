"""Looking after credentials after they are issued — docs/design.md §7.4, item ``#156``.

**A sibling of `authentication` rather than part of it**, and the import graph is what
decides that: `authorization` already imports `authentication` for its `Principal`, so
`authentication` cannot ask `authorization` who may do what. Revoking a token is an
authority question about an authentication object, so it belongs to neither and imports
both.

The column this writes has been read on every request since M1. `revoked_at` is checked at
authentication time rather than cached anywhere, which is why revocation is immediate and
why this module is short — the enforcement was never the missing part. Nothing could *set*
it, so an instance could issue credentials and never take one back.
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.instances
import subroutine.domain.schedule
import subroutine.domain.selection
import subroutine.domain.text
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions

#: The role a service account is given when one is created for it. The *narrowest* that can
#: actually work: an account with no role authenticates and can do nothing, which reads as
#: a broken token rather than as a missing membership.
SERVICE_ACCOUNT_ROLE = "contributor"



#: What a credential's title may hold, matching ``api_token.title``'s column — `SR#1555`.
#:
#: **The derived default is not measured against it, deliberately.** ``{username}'s token`` is
#: ours rather than the caller's, and a username is bounded at 64, so it cannot reach this;
#: :func:`subroutine.domain.text.fit` refuses *user input* and this project's rule is that a
#: derived value is shaped rather than validated.
MAX_TITLE_LENGTH = 128

def expires_on (
	written: str | None, *, timezone: str, now: datetime.datetime | None = None
) -> datetime.datetime | None:
	"""Read an expiry somebody wrote, as the last instant of the day it names.

	``None`` for nothing written, which is a credential that does not expire.

	**A whole day, and the credential works through the end of it** — the same reading a
	deadline gets (§6.5). A token that stopped at midnight starting the day somebody named is
	the kind of surprise that arrives at the worst moment.

	Here rather than in either transport, because both take one and the grammar has to be the
	same grammar: ``2026-09-01`` and ``now+30d`` mean what they mean whether they arrived on a
	command line or in a request body. The CLI had the only copy until `#208` gave the API an
	expiry to parse.
	"""

	if written is None or not written.strip():
		return None

	return subroutine.domain.schedule.interpret(
		written.strip(),
		boundary=subroutine.domain.schedule.Boundary.END,
		timezone=timezone,
		now=now or subroutine.db.types.utcnow(),
		field="expires_at",
	).instant


def issued_tokens (
	session: sqlalchemy.orm.Session, *, actor: subroutine.domain.authentication.Principal
) -> list[subroutine.db.models.identity.ApiToken]:
	"""Return the credentials this operator may see, newest first.

	**Narrowed the same way revocation is**, so a listing never shows something the caller
	could not act on — an inventory you can read and not revoke is a worse answer than one
	that is short.
	"""

	model = subroutine.db.models.identity.ApiToken
	statement = sqlalchemy.select(model).order_by(model.created_at.desc())

	if not _may_administer_credentials(actor):
		statement = statement.where(
			sqlalchemy.or_(
				model.user_id == actor.user.id, model.created_by == actor.user.id
			)
		)

	return list(session.scalars(statement))


def revoke (
	session: sqlalchemy.orm.Session,
	token: subroutine.db.models.identity.ApiToken,
	*,
	now: datetime.datetime | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.identity.ApiToken:
	"""Stop a credential working, from this instant, if this actor may.

	**The mechanism is `authentication.revoke_token` and is not repeated here.** That function
	has existed since M1, is idempotent, and keeps the first revocation time; this adds the
	only thing it could not, which is the authority question — `authorization` imports
	`authentication`, so the check cannot live beside the write.

	The first version of this module *did* repeat it, having been written without looking. It
	is the ordinary way a second implementation of something arrives.

	**Who may**: the person the token was issued *for*, the person who issued it, or an
	instance administrator. The first two are what the case this was built for needs — a
	month's work on somebody else's instance, where the issuer has to be able to take access
	back without anybody's help, and the holder has to be able to burn their own if it leaks.
	"""

	if actor is not None and not _may_revoke(actor, token):
		raise subroutine.errors.Forbidden(
			"Only the person a credential was issued for, the person who issued it, or an "
			"instance administrator may revoke it."
		)

	subroutine.domain.authentication.revoke_token(token, at=now)
	session.flush()

	return token


def _may_administer_credentials (actor: subroutine.domain.authentication.Principal) -> bool:
	"""Whether this principal may act on credentials that are nothing to do with them."""

	try:
		subroutine.domain.authorization.authorize_instance(
			actor, subroutine.permissions.INSTANCE_USER_CREATE
		)

	except subroutine.errors.SubroutineError:
		return False

	return True


def _may_revoke (
	actor: subroutine.domain.authentication.Principal, token: subroutine.db.models.identity.ApiToken
) -> bool:
	"""Whether this principal may revoke that credential."""

	if token.user_id == actor.user.id or token.created_by == actor.user.id:
		return True

	return _may_administer_credentials(actor)


def issue (
	session: sqlalchemy.orm.Session,
	*,
	actor: subroutine.domain.authentication.Principal,
	title: str | None = None,
	username: str | None = None,
	service_account: str | None = None,
	workspace: str | None = None,
	scopes: typing.Sequence[str] = (),
	projects: typing.Sequence[str] | None = None,
	writes: typing.Sequence[str] | None = None,
	expires: str | None = None,
) -> tuple[
	subroutine.db.models.identity.ApiToken,
	subroutine.db.models.identity.User,
	subroutine.auth.IssuedToken,
	bool,
]:
	"""Mint a credential from what somebody asked for, and return it with its owner — `#348`.

	**The whole of "issue a credential" in one place**, because it now has two callers: the
	router, and the local client behind `subroutine token create`. Until this existed the
	router held it and the command opened a database directly (§12.4), which was fine while
	every machine had a local database and became a wall the moment one did not.

	Everything here is *resolution* — a username to an account, a slug to a workspace, a key to
	a project, `now+30d` to an instant — and each step delegates to the function every other
	surface uses, so a token issued over HTTP and one issued locally are narrowed by the same
	rules. The narrowing itself, and the refusal to widen, stay in
	:func:`subroutine.domain.authentication.issue_token`.

	``projects`` takes keys or ids. The column stores ids, because a key can be renamed
	(`#176`) and a credential must not follow it onto whatever takes the old name.

	``writes`` is the same, one dimension down: where this credential may *change* things,
	within what ``projects`` lets it see (`#371`). ``None`` means its whole reach, so saying
	nothing leaves a credential exactly as it would have been before the distinction existed.

	``service_account`` names a machine identity and **creates one if there is none**, with a
	role it can actually work with. That whole sequence is here rather than in the caller
	because it is three writes — an account, a membership, a credential — and over a network it
	would otherwise be three requests with a half-finished agent if the second failed. One
	transaction, one call, whichever transport asked.

	The fourth element of the return says whether an account had to be made, because "created
	service account claude" is worth printing and cannot be inferred afterwards without a race.
	"""

	owner, created = _owner_for(
		session,
		actor,
		username=username,
		service_account=service_account,
		workspace=workspace,
	)
	pinned = (
		None
		if workspace is None
		else subroutine.domain.selection.workspace(session, actor, requested=workspace)
	)
	restricted = subroutine.domain.selection.token_projects(
		session, actor, projects, workspace=pinned
	)

	# **Resolved the same way, so `--project SR --write SR` cannot mean two different SRs**
	# (`#371`). Both go through the one resolver, which refuses an unknown key and an
	# ambiguous one; the *relationship* between the two lists — that a write set is inside
	# the reach — is checked by `issue_token`, where the ids are canonical.
	writable = subroutine.domain.selection.token_projects(
		session, actor, writes, workspace=pinned
	)

	row, issued = subroutine.domain.authentication.issue_token(
		session,
		user=owner,
		title=subroutine.domain.text.fit(title, field="title", limit=MAX_TITLE_LENGTH)
		if title
		else f"{owner.username}'s token",
		workspace_id=None if pinned is None else pinned.id,
		scopes=scopes,
		project_scope=(
			None if restricted is None else [str(found.id) for found in restricted]
		),
		project_write_scope=(
			None if writable is None else [str(found.id) for found in writable]
		),
		expires_at=expires_on(
			expires,
			timezone=subroutine.domain.schedule.zone_for(
				user=actor.user, instance=subroutine.domain.instances.get(session)
			),
		),
		created_by=actor.user.id,
		actor=actor,
	)

	return row, owner, issued, created


def _owner_for (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	*,
	username: str | None,
	service_account: str | None,
	workspace: str | None,
) -> tuple[subroutine.db.models.identity.User, bool]:
	"""Return whose credential this is, and whether an account had to be made for it.

	**Two arguments, because these are two decisions** (`#207`). ``username`` says *who*;
	``service_account`` says who *and* that a machine identity may be created for the name. One
	word answering both is what this was, and it got each of them wrong at an edge: a
	``--service-account`` naming a person issued that person's credential and said nothing,
	under a flag whose stated subject is machines.

	Naming an existing service account twice reuses it rather than refusing — issuing a second
	token for one agent is an ordinary thing to want, and "that name is taken" would be a
	strange thing to say about the account you asked for.
	"""

	wanted = (username or "").strip()
	machine = (service_account or "").strip()

	if wanted and machine:
		raise subroutine.errors.ValidationError(
			"Say either a username or a service account, not both.",
			errors=[
				subroutine.errors.FieldError(
					field="service_account",
					code="invalid_field_value",
					message="Only one of username and service_account may be given.",
					hint="A username issues for an account that already exists; a service "
					"account issues for a machine identity and creates one if there is none.",
				)
			],
		)

	if not wanted and not machine:
		return actor.user, False

	existing = _live_account(session, wanted or machine)

	if wanted:
		if existing is not None:
			return existing, False

		# **"Absent" and "deactivated" get different sentences**, because they have different
		# remedies and the wrong one wastes somebody's time in a way they cannot see: telling
		# the holder of a deactivated account to create it sends them at a name already taken.
		if _any_account(session, wanted) is not None:
			# **Not found, like an absent one, and the sentence carries the difference.** The
			# endpoint has answered 404 for both since M1 — deliberately, since "inactive is
			# as good as absent" here — and telling the two apart by *status* would both break
			# a published contract and turn this into a way of probing which usernames exist.
			raise subroutine.errors.NotFound(
				f"{wanted!r} is deactivated, so a credential issued for it would be refused "
				f"the first time it was used.",
				errors=[
					subroutine.errors.FieldError(
						field="username",
						code="not_found",
						message=f"{wanted!r} is deactivated and cannot hold a working credential.",
						hint="Reactivate the account first, or issue the credential for "
						"somebody else.",
					)
				],
			)

		raise subroutine.errors.NotFound(
			f"There is no account called {wanted!r} here.",
			errors=[
				subroutine.errors.FieldError(
					field="username",
					code="not_found",
					message=f"No usable account is called {wanted!r}.",
					hint=f"'subroutine user list' shows who there is, and 'subroutine user "
					f"create {wanted}' adds them. To create a machine identity instead, name a "
					f"service account.",
				)
			],
		)

	if existing is not None:
		# **A person is not a machine identity, and this used to hand out their credential.**
		# Refused rather than reused: the argument says what it is for, somebody passing it
		# meant it, and issuing a human's authority under it is a thing they would not choose.
		if not existing.is_service_account:
			raise subroutine.errors.ValidationError(
				f"{existing.username!r} is a person's account, not a machine identity.",
				errors=[
					subroutine.errors.FieldError(
						field="service_account",
						code="invalid_field_value",
						message=f"{existing.username!r} belongs to a person.",
						hint=f"Use '--username {existing.username}' to issue a credential for "
						f"them, or choose another name for the service account.",
					)
				],
			)

		return existing, False

	account = subroutine.domain.users.create(
		session, username=machine, is_service_account=True, actor=actor
	)
	home = subroutine.domain.selection.workspace(session, actor, requested=workspace)

	# An account with no role can authenticate and do nothing, which reads as a broken token
	# rather than as a missing membership. Given the narrowest role that can actually work.
	subroutine.domain.workspaces.add_member(
		session, home, account, role_key=SERVICE_ACCOUNT_ROLE, actor=actor
	)

	return account, True


def _live_account (
	session: sqlalchemy.orm.Session, username: str
) -> subroutine.db.models.identity.User | None:
	"""Return the account of that name a credential could actually be used with.

	**Inactive is as good as absent** (`#207`): ``authenticate`` refuses a token whose owner is
	not active, so issuing one for a deactivated account is dead on arrival — accepted,
	printed, stored, and refused the first time somebody tries it.
	"""

	model = subroutine.db.models.identity.User

	return session.scalars(
		sqlalchemy.select(model).where(
			model.username_normalized == subroutine.domain.users.normalize(username),
			model.deleted_at.is_(None),
			model.is_active.is_(True),
		)
	).one_or_none()


def _any_account (
	session: sqlalchemy.orm.Session, username: str
) -> subroutine.db.models.identity.User | None:
	"""Return the account of that name whether or not it could be used, for a better refusal."""

	model = subroutine.db.models.identity.User

	return session.scalars(
		sqlalchemy.select(model).where(
			model.username_normalized == subroutine.domain.users.normalize(username),
			model.deleted_at.is_(None),
		)
	).one_or_none()


def mine (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	id_or_prefix: str,
) -> subroutine.db.models.identity.ApiToken:
	"""Find a credential this caller may act on, or report that there is no such thing.

	Resolved out of :func:`issued_tokens`, which is the set they may already read — so a
	credential belonging to somebody else is *absent* rather than forbidden, and revoking
	discloses nothing a listing would not.
	"""

	wanted = id_or_prefix.strip()

	for candidate in issued_tokens(session, actor=actor):
		if candidate.token_prefix == wanted or str(candidate.id) == wanted:
			return candidate

	raise subroutine.errors.NotFound(
		f"No credential here answers to {wanted!r}.",
		errors=[
			subroutine.errors.FieldError(
				field="id_or_prefix",
				code="not_found",
				message=f"No credential you can act on is {wanted!r}.",
				hint="'subroutine token list' prints the prefix of each one, which is what "
				"revoking takes.",
			)
		],
	)


def owners (
	session: sqlalchemy.orm.Session,
	rows: typing.Sequence[subroutine.db.models.identity.ApiToken],
) -> dict[uuid.UUID, subroutine.db.models.identity.User]:
	"""Return the accounts these credentials belong to, in one query rather than per row.

	Beside the listing it serves rather than in the router, since both transports render the
	same view and a listing that fetched an owner per row would be §8.4's N+1 in the one place
	an instance's whole credential inventory is read.
	"""

	wanted = {row.user_id for row in rows}

	if not wanted:
		return {}

	model = subroutine.db.models.identity.User

	return {
		found.id: found
		for found in session.scalars(sqlalchemy.select(model).where(model.id.in_(wanted)))
	}
