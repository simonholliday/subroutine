"""Calendar feeds: minting one, resolving one from its URL, and what it shows.

A feed is the fourth kind of credential and the only one whose secret travels in a path
(docs/design.md §20.2). Everything about that is decided there and in decision `#972`; what lives
here is the lifecycle and the query.

**The query is the part worth reading before changing anything.** A feed borrows its owner's
sight *at render time* rather than at creation, so every poll asks what that person may see
now — which is what stops a feed serving a private project after its owner was removed from
it, a leak nothing would ever surface because there is no login to audit.
"""

import dataclasses
import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.scoping
import subroutine.errors

#: How far back a feed reaches, from the moment of the request — Simon's decision of
#: 2026-08-17, recorded in `#972`.
#:
#: **The past is kept because most clients drop an event the moment a feed stops sending it**,
#: so excluding finished work would mean ticking a meeting off deletes it from a calendar's
#: history — a to-do list's rule producing a result no calendar has.
#:
#: Seven rather than ninety, and the number is doing work: an `.ics` has no honest way to say
#: an event is over (`STATUS:COMPLETED` is a ``VTODO`` property, and §20.4 refuses ``VTODO``
#: because the three major clients variously ignore it), so a finished event and a coming one
#: look alike. A week is short enough that the ambiguity is small.
PAST_DAYS = 7

#: And forward. It costs almost nothing now a `schedule` series is one ``RRULE`` rather than
#: four hundred rows, and it clears a year so an annual event is always present.
FUTURE_DAYS = 400

#: Both are constants rather than settings, and per-feed is deliberately not built: Simon
#: raised it as a *future* possibility and a column nobody writes to is §6.16's own refusal.
#: Adding ``past_days`` to the table later is additive, which is why nothing here anticipates
#: it — but they live together so that change is one edit.
PREFIX_ATTEMPTS = 8


@dataclasses.dataclass(frozen=True)
class Occasion:
	"""One thing a feed shows: a task, and which of its dates put it there.

	**A task can be here twice**, and §20.4 says why: the day you meant to do it and the day
	it is due are different facts, and a calendar showing one would hide the other. So this
	names the *field* as well as the row, and the pair is what makes a stable ``UID``.
	"""

	task: subroutine.db.models.work.Task
	field: str

	#: The rule this occasion repeats on, or ``None``. Only a ``schedule``-anchored series
	#: has one — decision `#972` §1: an ``RRULE`` describes a grid, and a completion-anchored
	#: series has no grid, because its dates are a function of when somebody acts.
	rule: str | None = None


def create (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	*,
	workspace_id: uuid.UUID,
	owner: subroutine.db.models.identity.User,
	title: str,
	audience: str = "everything",
	project_id: uuid.UUID | None = None,
	item_type_ids: typing.Sequence[uuid.UUID] | None = None,
	expires_at: datetime.datetime | None = None,
	now: datetime.datetime | None = None,
) -> tuple[subroutine.db.models.identity.CalendarFeed, subroutine.auth.IssuedToken]:
	"""Mint a feed and return it with the one readable form of its secret.

	The secret exists in readable form exactly once, here, which is why this returns the pair
	rather than storing it: §20.3 prints the URL once, like a token, and it cannot be
	recovered afterwards.
	"""

	moment = now if now is not None else subroutine.db.types.utcnow()

	if audience not in subroutine.db.models.identity.CALENDAR_AUDIENCES:
		offered = ", ".join(subroutine.db.models.identity.CALENDAR_AUDIENCES)

		raise subroutine.errors.ValidationError(
			f"There is no calendar audience called {audience!r}.",
			code="invalid_field_value",
			errors=[
				subroutine.errors.FieldError(
					field="audience",
					code="invalid_field_value",
					message=f"Valid audiences are: {offered}.",
				)
			],
		)

	_refuse_a_credential_that_would_be_widened(actor, expires_at=expires_at, now=moment)

	minted = _mint_unused_secret(session)
	feed = subroutine.db.models.identity.CalendarFeed(
		workspace_id=workspace_id,
		project_id=project_id,
		owner_id=owner.id,
		audience=audience,
		item_type_ids=None if item_type_ids is None else [str(one) for one in item_type_ids],
		title=title,
		token_prefix=minted.prefix,
		token_hash=minted.token_hash,
		expires_at=expires_at,
	)

	session.add(feed)
	session.flush()

	return feed, minted


def resolve (
	session: sqlalchemy.orm.Session,
	presented: str,
	*,
	now: datetime.datetime | None = None,
	record_poll: bool = True,
) -> subroutine.db.models.identity.CalendarFeed:
	"""Turn the secret out of a feed URL into the feed it names.

	**Every refusal is the same refusal**, deliberately, and for the reason
	:func:`~subroutine.domain.authentication.authenticate` gives: telling an unknown prefix
	from a wrong secret tells somebody when they have guessed half a credential. A revoked
	and an expired feed join them here, where a token distinguishes those — because a token
	is held by somebody who can be told why, and a feed URL is polled by a program that
	cannot act on the difference.
	"""

	parsed = subroutine.auth.parse_token(presented, kind=subroutine.auth.CALENDAR_KIND)

	if parsed is None:
		raise _unknown()

	prefix, secret = parsed
	moment = now if now is not None else subroutine.db.types.utcnow()

	model = subroutine.db.models.identity.CalendarFeed
	feed = session.scalars(
		sqlalchemy.select(model).where(model.token_prefix == prefix)
	).one_or_none()

	if feed is None or not subroutine.auth.token_matches(secret, feed.token_hash):
		raise _unknown()

	if feed.revoked_at is not None:
		raise _unknown()

	if feed.expires_at is not None and feed.expires_at <= moment:
		raise _unknown()

	owner = session.get(subroutine.db.models.identity.User, feed.owner_id)

	# **The owner's standing is checked on every poll, not at creation** (§20.1, and `#475`'s
	# rule that an account which has left stops working). A feed whose owner is gone has no
	# visibility rule left to apply, and falling back to *something* is how a leak survives
	# somebody being offboarded.
	if owner is None or owner.deleted_at is not None or not owner.is_active:
		raise _unknown()

	if record_poll:
		feed.last_polled_at = moment

	return feed


def reset (
	session: sqlalchemy.orm.Session, feed: subroutine.db.models.identity.CalendarFeed
) -> subroutine.auth.IssuedToken:
	"""Give a feed a new secret, so the URL somebody had stops working immediately.

	**The row survives and the subscription does not**, which is the whole point (§20.3): a
	leaked URL is fixed without losing the feed's scope, its audience or the record of when
	it was last polled. Revoking and creating another would lose all three and hand back a
	different id.
	"""

	minted = _mint_unused_secret(session)

	feed.token_prefix = minted.prefix
	feed.token_hash = minted.token_hash

	return minted


def revoke (
	session: sqlalchemy.orm.Session,
	feed: subroutine.db.models.identity.CalendarFeed,
	*,
	now: datetime.datetime | None = None,
) -> None:
	"""Stop a feed for good. Repeating it is not an error and does not move the date."""

	if feed.revoked_at is None:
		feed.revoked_at = now if now is not None else subroutine.db.types.utcnow()


def feeds (
	session: sqlalchemy.orm.Session,
	owner: subroutine.db.models.identity.User,
	*,
	workspace_id: uuid.UUID | None = None,
	include_revoked: bool = False,
) -> list[subroutine.db.models.identity.CalendarFeed]:
	"""Return this person's feeds, newest first.

	**Their own and nobody else's.** A list of somebody's feeds says which projects they
	watch and from how many devices, and §20.6 already accepts that a feed URL is a bearer
	credential nobody can audit — an inventory of them is the map that makes one worth
	stealing. Reading another person's is not a permission this offers to anybody.
	"""

	model = subroutine.db.models.identity.CalendarFeed
	statement = sqlalchemy.select(model).where(model.owner_id == owner.id)

	if workspace_id is not None:
		statement = statement.where(model.workspace_id == workspace_id)

	if not include_revoked:
		statement = statement.where(model.revoked_at.is_(None))

	return list(session.scalars(statement.order_by(model.created_at.desc())))


def occasions (
	session: sqlalchemy.orm.Session,
	feed: subroutine.db.models.identity.CalendarFeed,
	*,
	now: datetime.datetime | None = None,
) -> list[Occasion]:
	"""Return everything this feed shows, as one row per date rather than per task.

	**Narrowed by the owner, through `scoping.readable_tasks` like every other listing.** The
	principal is built with the feed in it, so it is not `is_local` and reports `task:read` —
	`#364`'s rule, and the reason `Principal` has a slot for this at all.
	"""

	moment = now if now is not None else subroutine.db.types.utcnow()
	owner = session.get(subroutine.db.models.identity.User, feed.owner_id)

	if owner is None:
		return []

	principal = subroutine.domain.authentication.Principal(user=owner, feed=feed)
	task = subroutine.db.models.work.Task

	# **Completed work is included and templates are asked for**, which is where this differs
	# from every other listing — decision `#972` §4 and §1. A calendar is not a work queue: it
	# keeps the recent past, and a `schedule`-anchored series exists only as its template.
	statement = subroutine.domain.scoping.readable_tasks(
		principal,
		workspace_ids=[feed.workspace_id],
		include_completed=True,
		include_templates=True,
	)

	if feed.project_id is not None:
		# **Through `scoping.within_project`, which is the rule every listing already uses**
		# — so a feed on a parent covers what is filed underneath it, exactly as
		# `list --project` does since `#320`. §20.1 requires the subtree for its own reason
		# (§7.3a's privacy inherits down the tree, so a feed that stopped at the parent would
		# show less than that project's page), and the two arriving at one predicate is what
		# stops them drifting. It is over the *project's* path, which `readable_tasks` has
		# already joined.
		scope = session.get(subroutine.db.models.project.Project, feed.project_id)

		if scope is None:
			return []

		statement = statement.where(subroutine.domain.scoping.within_project(scope))

	if feed.audience == "assigned_to_me":
		statement = statement.where(task.assignee_id == feed.owner_id)

	if feed.item_type_ids is not None:
		statement = statement.where(
			task.type_id.in_([uuid.UUID(one) for one in feed.item_type_ids])
		)

	earliest = moment - datetime.timedelta(days=PAST_DAYS)
	latest = moment + datetime.timedelta(days=FUTURE_DAYS)

	found: list[Occasion] = []

	for row in session.scalars(statement):
		found.extend(_occasions_of(row, earliest=earliest, latest=latest))

	return found


def _occasions_of (
	row: subroutine.db.models.work.Task,
	*,
	earliest: datetime.datetime,
	latest: datetime.datetime,
) -> list[Occasion]:
	"""Return the dates one task puts on a calendar, which may be none, one or two."""

	# **A template is here only to carry a rule** (`#972` §1), and only a `schedule`-anchored
	# one: a completion-anchored series has no grid, so its template describes nothing a
	# client could expand and its live instance is what appears instead.
	if row.is_template:
		if row.recurrence_anchor != "schedule" or not row.recurrence_rule:
			return []

		return [
			Occasion(task=row, field=field, rule=row.recurrence_rule)
			for field in ("starts_at", "due_at")
			if getattr(row, field) is not None
		]

	# **A repeating instance is not given the rule**, because the template already carried it
	# for a `schedule` series and a `completion` one has none to give. Emitting both would put
	# the same series on a calendar twice, once as a grid it does not follow.
	found = []

	for field in ("starts_at", "due_at"):
		when = getattr(row, field)

		if when is not None and earliest <= when <= latest:
			found.append(Occasion(task=row, field=field))

	return found


def _unknown () -> subroutine.errors.NotFound:
	"""Return the one refusal this endpoint gives, whatever was actually wrong.

	**A 404 rather than a 401**, because the credential is the address: there is no header to
	correct and no `WWW-Authenticate` challenge a calendar client could answer. A URL that
	does not name a live feed simply does not name anything, which is also what a revoked one
	should look like to whoever it leaked to.
	"""

	return subroutine.errors.NotFound("There is no calendar at that address.")


def _refuse_a_credential_that_would_be_widened (
	actor: subroutine.domain.authentication.Principal | None,
	*,
	expires_at: datetime.datetime | None,
	now: datetime.datetime,
) -> None:
	"""Refuse a feed that would hand back more than the credential asking for it.

	**`#837`'s rule, and `#829`'s test asked of a fourth credential**: can this be issued
	wider than the thing asking, and can something narrower be exchanged for it. A feed
	renders with the *owner's* visibility rather than the presenter's narrowing (§20.1), so a
	credential scoped to one project could mint a URL reading the whole workspace — the
	escalation `#829` found on `POST /v1/login-links`, one credential kind along.

	**Refused outright rather than checked against the scope**, which is `#829`'s own answer
	and is deliberately the blunter of the two. A narrower rule — *the feed's scope must lie
	inside the presenter's reach* — is available and is strictly more permissive; loosening
	to it later is a deliberate act, where discovering the blunt version was needed is a leak.
	Nothing is known to have met this wall.

	``None`` and `is_local` are §12.1a, somebody at a terminal with the database file, which
	no check here narrows — and is what keeps ``subroutine calendar create`` working for a
	self-hoster who has issued themselves no token at all.
	"""

	if actor is None or actor.is_local:
		return

	if actor.narrows:
		raise subroutine.errors.Forbidden(
			"A bounded credential cannot mint a calendar feed.",
			hint="A feed reads with its owner's own sight rather than with the narrowing on "
			"the credential that made it, so this would hand back more than you presented. "
			"Use an unrestricted credential, or run 'subroutine calendar create' at the "
			"instance itself.",
		)

	# **`#356`'s rule, only in the amplifying direction.** A credential with no expiry may
	# mint anything; one that outlives the feed is not being widened. Only a feed that would
	# outlive its credential is refused — and a feed's expiry is *optional*, so the common
	# mistake is a permanent URL minted by a token that stops working in a month.
	if actor.expires_at is None:
		return

	if expires_at is None or expires_at > actor.expires_at:
		raise subroutine.errors.Forbidden(
			"A calendar feed would outlive the credential that asked for it.",
			hint="Give the feed an expiry no later than the credential's, or mint it with a "
			"credential that does not expire.",
		)


def _mint_unused_secret (
	session: sqlalchemy.orm.Session,
) -> subroutine.auth.IssuedToken:
	"""Generate a feed secret whose prefix is not already in use."""

	model = subroutine.db.models.identity.CalendarFeed

	for _attempt in range(PREFIX_ATTEMPTS):
		issued = subroutine.auth.generate_token(kind=subroutine.auth.CALENDAR_KIND)

		taken = session.scalars(
			sqlalchemy.select(model.id).where(model.token_prefix == issued.prefix)
		).first()

		if taken is None:
			return issued

	raise RuntimeError(
		f"Could not find an unused calendar prefix in {PREFIX_ATTEMPTS} attempts. This "
		f"should be impossible; check that the random number source is working."
	)
