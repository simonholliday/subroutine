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
import subroutine.domain.authorization
import subroutine.domain.instances
import subroutine.domain.schedule
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.domain.tasks
import subroutine.domain.tokens
import subroutine.errors
import subroutine.permissions

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

	#: Slots the rule describes that no longer hold anything, rendered as ``EXDATE`` (`#1248`).
	#: Only ever set beside a ``rule``: a grid is the only thing that can have a hole in it.
	emptied: tuple[datetime.datetime, ...] = ()


def create (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
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

	**A feed is always owned by whoever asked for it, and that is structural rather than a
	rule a caller keeps.** An ``owner`` parameter would let one person mint a URL that renders
	with *somebody else's* sight and hand it to themselves — the escalation `#829` found on
	sign-in links, which is worse here because a feed has no login and nothing to audit. There
	is no field for it and so nothing to check.
	"""

	moment = now if now is not None else subroutine.db.types.utcnow()
	owner = actor.user

	# **Asked as `task:read`, because that is exactly what the feed will do** — §20.1 says a
	# feed shows what its owner may see, and this is the permission that decides that. It is
	# not asked of the *project*: a feed on a project nobody may see mints happily and renders
	# nothing, which is `scoping`'s answer everywhere else and is why §7.3a's privacy is a
	# property of the query rather than of the credential.
	subroutine.domain.authorization.authorize(
		session, actor, subroutine.permissions.TASK_READ, workspace_id=workspace_id
	)

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


def refuse_when_disabled (enabled: bool) -> None:
	"""Refuse to put a new feed URL into the world when the feature is off — `#1068`.

	**Simon's decision of 2026-08-22: ``calendars_enabled`` is a kill switch for the feature,
	not only for serving.** `docs/hosting.md` has always described it as *"whether this instance
	serves calendar feeds at all"*, and only the feed route read it — so an operator who had
	turned feeds off could still be handed a URL, and it answered 404 for ever. `docs/connecting.md`
	told that person *"if the command is refused outright, feeds may be turned off on that
	instance"*, describing a symptom that could not occur.

	**Minting and resetting only.** Revoking and listing keep working with the feature off, and
	that is the decision rather than an oversight: turning something off must never be a way to
	trap a live credential. An operator who disables feeds *because* one leaked would otherwise
	be unable to end it — and you cannot revoke what you cannot list. The switch governs the two
	acts that put a working credential into the world; ending one is neither.

	**Here rather than in either transport**, for the reason :func:`issue` itself is: the CLI
	reaches the domain directly and the API reaches it through a route, and a check written once
	per transport is two statements that must agree.

	``enabled`` is required rather than defaulted, deliberately. A default is what lets one
	caller forget and look identical to one that was never meant to be guarded — `#909`'s
	lesson, where an argument in two halves drifted because the second had one.
	"""

	if enabled:
		return

	raise subroutine.errors.Forbidden(
		"Calendar feeds are turned off on this instance.",
		hint="An operator can set 'calendars_enabled = true' in config.toml and restart. "
		"Existing feeds can still be listed and revoked.",
	)


def issue (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	*,
	title: str,
	workspace: str | None = None,
	project: str | None = None,
	audience: str = "everything",
	item_types: typing.Sequence[str] | None = None,
	expires: str | None = None,
	enabled: bool,
	now: datetime.datetime | None = None,
) -> tuple[subroutine.db.models.identity.CalendarFeed, subroutine.auth.IssuedToken]:
	"""Mint a feed from what somebody typed, resolving each name to what it points at.

	**Here rather than in either transport**, for the reason :func:`subroutine.domain.tokens.issue`
	is: both take the same words and the words have to mean the same thing. ``WEB`` is one
	project whether it arrived on a command line or in a request body, and a resolver per
	transport is the divergence S3-07 removed for the task shape.

	**A workspace is required in the sense that one is always chosen**, and left unnamed it is
	resolved the way every other request resolves it — refused with the alternatives where the
	caller can reach more than one. A feed spanning workspaces could not exist anyway: refs
	collide across them (§6.2), so two items would share an event identity.
	"""

	# Before anything is resolved, so a disabled instance answers the same way whatever else is
	# wrong with the request — a refusal that depended on the workspace existing would be two
	# answers to one question.
	refuse_when_disabled(enabled)

	found = subroutine.domain.selection.workspace(session, actor, requested=workspace)
	scope = (
		None
		if project is None or not project.strip()
		else subroutine.domain.selection.addressed(
			session, actor, found, project.strip(), field="project"
		)
	)
	types = None

	if item_types is not None:
		named = [key.strip() for key in item_types if key.strip()]

		# **An empty filter is refused rather than read as no filter at all.** ``None`` means
		# every type, so collapsing ``[]`` into it would answer *show me nothing* with *show me
		# everything* — a plausible, complete, wrong answer on a feed nobody reads closely
		# enough to notice, and the shape `#818` is this repository's worked example of.
		if not named:
			raise subroutine.errors.ValidationError(
				"A calendar that shows no item types would show nothing at all.",
				code="invalid_field_value",
				errors=[
					subroutine.errors.FieldError(
						field="item_types",
						code="invalid_field_value",
						message="Name at least one type, or leave this out for all of them.",
					)
				],
			)

		types = [
			subroutine.domain.tasks.item_type_for(
				session, found.id, key, field="item_types"
			).id
			for key in named
		]

	return create(
		session,
		actor,
		workspace_id=found.id,
		title=title,
		audience=audience,
		project_id=None if scope is None else scope.id,
		item_type_ids=types,
		expires_at=subroutine.domain.tokens.expires_on(
			expires,
			timezone=subroutine.domain.schedule.zone_for(
				user=actor.user,
				workspace=found,
				instance=subroutine.domain.instances.get(session),
			),
			now=now,
		),
		now=now,
	)


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

	# **And the workspace's standing, for exactly the same reason** (`#704`). A feed is the
	# one credential that reaches tasks without going through ``workspaces.readable``, which
	# is where every other surface stops at the trash — it holds a ``workspace_id`` and asks
	# ``readable_tasks`` directly. So until a workspace could be deleted this could not be
	# wrong, and the moment one can it is the single URL that goes on serving a tenancy
	# nobody can otherwise see. Measured rather than reasoned about: the first version of
	# `#704` shipped the delete and left this poll answering with the deleted workspace's
	# whole calendar.
	workspace = session.get(subroutine.db.models.identity.Workspace, feed.workspace_id)

	if workspace is None or workspace.deleted_at is not None:
		raise _unknown()

	if record_poll:
		feed.last_polled_at = moment

	return feed


def reset (
	session: sqlalchemy.orm.Session,
	feed: subroutine.db.models.identity.CalendarFeed,
	*,
	enabled: bool,
) -> subroutine.auth.IssuedToken:
	"""Give a feed a new secret, so the URL somebody had stops working immediately.

	**The row survives and the subscription does not**, which is the whole point (§20.3): a
	leaked URL is fixed without losing the feed's scope, its audience or the record of when
	it was last polled. Revoking and creating another would lose all three and hand back a
	different id.

	**Refused when the feature is off** (`#1068`), because a reset mints a working URL exactly
	as :func:`issue` does. :func:`refuse_when_disabled` carries the whole argument.
	"""

	refuse_when_disabled(enabled)

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


def mine (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	id_or_prefix: str,
) -> subroutine.db.models.identity.CalendarFeed:
	"""Find a feed this caller may act on, or report that there is no such thing.

	Resolved out of :func:`feeds`, which is the set they may already read — so somebody
	else's feed is *absent* rather than forbidden, and resetting discloses nothing a listing
	would not. `tokens.mine` one credential kind along, and the same reasoning.

	**Revoked feeds are searched too**, unlike the listing's default. Revoking twice should
	say *already revoked* rather than *no such thing*, and somebody reading a revoked feed's
	id off a listing they asked to include them must be able to name it.
	"""

	wanted = id_or_prefix.strip()

	for candidate in feeds(session, actor.user, include_revoked=True):
		if candidate.token_prefix == wanted or str(candidate.id) == wanted:
			return candidate

	raise subroutine.errors.NotFound(
		f"No calendar here answers to {wanted!r}.",
		errors=[
			subroutine.errors.FieldError(
				field="id_or_prefix",
				code="not_found",
				message=f"No calendar of yours is {wanted!r}.",
				hint="'subroutine calendar list' prints the reference of each one, which is "
				"what resetting and revoking take.",
			)
		],
	)


def address (base: str | None, minted: subroutine.auth.IssuedToken) -> str | None:
	"""Return the URL a calendar application subscribes to, or ``None``.

	**``None`` when the instance has not been told its own ``public_url``**, rather than a
	guess assembled from wherever the request happened to arrive. That is the same refusal
	:func:`subroutine.api.calendars._addresses` makes about an item's link and `#832`'s about
	what an instance may infer about itself — and here it matters more, because the guess
	*is* the credential: a URL naming the wrong host is one somebody pastes into a calendar
	application, which then sends the secret there every fifteen minutes for ever.

	The path is built from the credential's two halves rather than from the whole string, so
	this and the route that reads it back cannot disagree about where the split is.
	"""

	if not base:
		return None

	secret = minted.value.get_secret_value()
	parsed = subroutine.auth.parse_token(secret, kind=subroutine.auth.CALENDAR_KIND)

	if parsed is None:
		raise RuntimeError(
			"A credential this program just minted did not parse as one. This should be "
			"impossible; check that 'generate_token' and 'parse_token' still agree."
		)

	prefix, rest = parsed

	return f"{str(base).rstrip('/')}/v1/calendars/{prefix}/{rest}.ics"


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
	rows = list(session.scalars(statement))

	# **Which templates put a grid in this feed** (`#1067`). A `schedule` series was emitted
	# twice: the template as an `RRULE` covering every occurrence, and its live instance again
	# as a standalone event on a date the grid already carries. The changelog says a repeating
	# item arrives "as a repeating event rather than as several hundred copies"; it arrived as
	# a repeating event plus a copy.
	#
	# **Asked of `_occasions_of` rather than re-derived from the columns**, so the rule for
	# *does this template emit anything* lives in one place. It is asked twice, which costs
	# nothing and is what stops the two copies disagreeing.
	gridded = {
		row.id
		for row in rows
		if row.is_template and _occasions_of(row, earliest=earliest, latest=latest)
	}

	emptied = _emptied_slots(session, principal, rows, gridded, workspace_id=feed.workspace_id)

	for row in rows:
		if row.is_template:
			found.extend(
				_occasions_of(
					row,
					earliest=earliest,
					latest=latest,
					emptied=tuple(sorted(emptied.get(row.id, ()))),
				)
			)

		elif not _is_on_its_grid(row, gridded):
			found.extend(_occasions_of(row, earliest=earliest, latest=latest))

	return found


def _emptied_slots (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	rows: typing.Sequence[subroutine.db.models.work.Task],
	gridded: set[uuid.UUID],
	*,
	workspace_id: uuid.UUID,
) -> dict[uuid.UUID, set[datetime.datetime]]:
	"""Return, per template, the grid slots that no longer hold an occurrence (`#1248`).

	**A rule with a hole in it needs the hole said out loud.** A client expands the ``RRULE``
	and draws every slot it describes, so a slot whose occurrence has gone elsewhere — or gone
	— is a meeting in somebody's calendar that is not happening, and it is the one that looks
	normal. RFC 5545's ``EXDATE`` is how you say it, and ``occurrence_at`` is already exactly
	the value it needs: *the slot the series minted this row for*, kept unchanged when the date
	moves, which is what lets :func:`_is_on_its_grid` see a move at all.

	**Two ways a slot empties, and they had to be settled together** rather than one of them
	being discovered next: the occurrence was **moved**, and the occurrence was **deleted**.
	Both were measured before this was written, and both left the phantom.

	The deleted half needs its own query, because the feed's own read deliberately excludes
	deleted rows — they are not work any more and must not appear as events. Narrowed to the
	templates that actually put a grid in this feed, so a feed with no repeating items asks
	nothing extra.

	**Restored is answered for free.** Nothing is stored; this is computed per request from the
	rows as they are, so taking an occurrence back out of the trash refills its slot and the
	``EXDATE`` stops being emitted.
	"""

	emptied: dict[uuid.UUID, set[datetime.datetime]] = {}

	for row in rows:
		template_id = row.recurrence_template_id

		if row.is_template or template_id not in gridded:
			continue

		if not _is_on_its_grid(row, gridded) and row.occurrence_at is not None:
			emptied.setdefault(template_id, set()).add(row.occurrence_at)

	if not gridded:
		return emptied

	task = subroutine.db.models.work.Task
	discarded = subroutine.domain.scoping.readable_tasks(
		principal, workspace_ids=[workspace_id], include_deleted=True, include_completed=True
	).where(task.deleted_at.is_not(None), task.recurrence_template_id.in_(gridded))

	for row in session.scalars(discarded):
		if row.occurrence_at is not None and row.recurrence_template_id is not None:
			emptied.setdefault(row.recurrence_template_id, set()).add(row.occurrence_at)

	return emptied


def _is_on_its_grid (row: subroutine.db.models.work.Task, gridded: set[uuid.UUID]) -> bool:
	"""Say whether this occurrence is already described by a rule emitted in this feed.

	**Skipped only when it duplicates, which is not the same as *belongs to a series***. An
	occurrence somebody has moved is no longer on the grid, so the rule does not describe where
	it actually is — dropping it would leave the calendar showing the date it was *going* to be
	on, which is worse than the duplicate this exists to remove.

	``occurrence_at`` is the slot the series minted this row for, and rescheduling changes the
	date without touching it, so the two parting company is exactly *this has been moved*.

	**The case it cannot see**, written down rather than left to be discovered: a series
	carrying both a start and a deadline records one ``occurrence_at`` — the column
	:func:`~subroutine.domain.tasks.grid_field` names — so moving only the *start* of such a
	series is a move this reads as none. The feed then shows the grid's start rather than the
	moved one. Narrow enough to accept and too specific to guess at; what it wants is
	`RECURRENCE-ID` overrides, which need a per-field original this schema does not keep.
	"""

	if row.recurrence_template_id not in gridded:
		return False

	return row.occurrence_at is not None and (
		row.occurrence_at == subroutine.domain.tasks.grid_date(row)
	)


def _occasions_of (
	row: subroutine.db.models.work.Task,
	*,
	earliest: datetime.datetime,
	latest: datetime.datetime,
	emptied: tuple[datetime.datetime, ...] = (),
) -> list[Occasion]:
	"""Return the dates one task puts on a calendar, which may be none, one or two.

	``emptied`` is meaningful only for a template, and only on the field ``occurrence_at``
	follows — see :func:`~subroutine.domain.tasks.grid_field`.

	**A series with both dates has two grids and one recorded slot**, so only one of them can
	be excluded honestly: its deadline grid is corrected and its start grid keeps the phantom.
	The same limitation :func:`_is_on_its_grid` records, in the matching direction.
	"""

	# **A template is here only to carry a rule** (`#972` §1), and only a `schedule`-anchored
	# one: a completion-anchored series has no grid, so its template describes nothing a
	# client could expand and its live instance is what appears instead.
	if row.is_template:
		if row.recurrence_anchor != "schedule" or not row.recurrence_rule:
			return []

		anchoring = subroutine.domain.tasks.grid_field(row)

		return [
			Occasion(
				task=row,
				field=field,
				rule=row.recurrence_rule,
				emptied=emptied if field == anchoring else (),
			)
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
	actor: subroutine.domain.authentication.Principal,
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

	`is_local` is §12.1a, somebody at a terminal with the database file, which no check here
	narrows — and is what keeps ``subroutine calendar create`` working for a self-hoster who
	has issued themselves no token at all.
	"""

	if actor.is_local:
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
