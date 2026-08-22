"""Calendar feeds: minting and revoking them, and the one address a calendar application fetches.

Everything about the shape is decided in docs/design.md §20 and in decision `#972`; what is here is
HTTP. **Two kinds of route live here and they authenticate differently**, which is why they
share a module: the lifecycle three are ordinary credentialled endpoints, and the feed itself
is the only public route in this application that reads somebody's work.

Four properties of the feed route are unlike every other one in the application, and each is
a deliberate consequence of the secret being in the path rather than in a header:

* **It carries no ``PrincipalDep``**, so §7.7's limiters — which live inside that dependency —
  do not reach it. `#364` predicted that gap for a login endpoint; this is its second
  instance, and :meth:`~subroutine.api.limits.Limiters.count_a_poll` is the answer.
* **Every refusal is a 404.** The credential *is* the address, so there is no header to
  correct and no challenge a calendar client could answer.
* **It answers ``text/calendar``**, and conditionally: clients poll on schedules nobody here
  controls and most polls change nothing.
* **It can be turned off entirely.** ``calendars_enabled`` is §20.6's kill switch, and when it
  is off this answers 404 exactly as it does for an address naming nothing — so an instance
  with the feature off is indistinguishable from one that never had a feed.

**The switch is checked here rather than at mounting**, and that is deliberate. ``ROUTERS`` is
declared as data so that ``routing.check``, ``routing.declarations`` and
``tests/test_api_authentication.py`` can all read what this application answers; a route
mounted only sometimes would be invisible to every one of them, including the walk that says
which routes are public. A runtime 404 costs a settings lookup and keeps all three honest.
"""

import hashlib
import typing

import fastapi
import sqlalchemy.orm
import starlette.requests
import starlette.responses

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.calendars
import subroutine.domain.icalendar
import subroutine.domain.instances
import subroutine.errors
import subroutine.views

#: §20.5. A quarter of an hour is roughly the fastest any major client refreshes anyway, so a
#: shorter one buys nothing and a longer one makes a change somebody just made look lost.
#:
#: ``private`` because the response is one person's work: a shared cache holding it would serve
#: it to whoever asked next, and the URL is the only thing saying who may.
CACHE_CONTROL = "private, max-age=900"

#: What the feed is served as. ``charset`` is explicit because RFC 5545 files are UTF-8 and a
#: client guessing Latin-1 renders an em dash as two characters of nonsense.
CONTENT_TYPE = "text/calendar; charset=utf-8"

router = fastapi.APIRouter(
	prefix="/v1/calendars",
	tags=["calendars"],
	route_class=subroutine.api.routing.Transactional,
)


#: What ``?fields=`` may name, read off the view so the two cannot drift (docs/design.md §14.10).
SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Calendar)


class Create(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/calendars`` accepts.

	**There is no owner field, deliberately.** A feed renders with its owner's sight (§20.1),
	so naming somebody else would mint a URL that reads their work and hand it to whoever
	asked — the escalation `#829` found on sign-in links, and worse here because a feed has no
	session to end and nothing to audit. The owner is the caller, structurally.
	"""

	#: What this feed is for. Shown by the listing, and it is the only thing telling two
	#: subscriptions apart once they are in somebody's calendar application.
	title: str

	#: Which workspace's work it shows. Omitted means the caller's only one, refused with the
	#: alternatives where there is a choice — every other request resolves it that way.
	workspace: str | None = None

	#: Narrow it to one project and everything filed underneath. Omitted means the workspace.
	project: str | None = None

	#: ``everything`` or ``assigned_to_me`` (§20.1).
	audience: str = "everything"

	#: The task types to show, by the keys ``GET /v1/meta`` publishes. Omitted means all of
	#: them; an empty list is refused rather than read as *all*, because it means the opposite.
	item_types: list[str] | None = None

	#: When it should stop working, as a whole day — ``2026-09-01`` or ``now+30d``. The same
	#: grammar a credential's expiry takes, because it is one.
	expires: str | None = None


@router.post("", status_code=201, summary="Create a calendar feed")
def create (
	body: Create,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
) -> subroutine.views.IssuedCalendar:
	"""Mint a feed and return its URL once.

	**The URL is the credential and it is in this response and in nothing else, ever.** Only a
	hash of the secret is stored, so nothing recovers it afterwards — including this instance.
	Give it to a calendar application when you receive it, and reset the feed if it leaks.

	``url`` is null when this instance has not been told its own ``public_url``. That is not a
	failure to build one: the whole URL is the secret, so a host guessed from a request header
	would send it wherever that header pointed, every fifteen minutes, for as long as the
	subscription lives.

	A bounded credential cannot mint one. A feed reads with its owner's own sight rather than
	with the narrowing on whatever asked for it, so issuing one from a restricted token would
	hand back more than was presented.
	"""

	feed, minted = subroutine.domain.calendars.issue(
		session,
		actor,
		title=body.title,
		workspace=body.workspace,
		project=body.project,
		audience=body.audience,
		item_types=body.item_types,
		expires=body.expires,
	)
	rendered = subroutine.views.calendar(
		feed,
		url=subroutine.domain.calendars.address(settings.public_url, minted),
		issued=True,
		session=session,
		principal=actor,
	)

	assert isinstance(rendered, subroutine.views.IssuedCalendar)

	return rendered


@router.get(
	"",
	summary="List your calendar feeds",
	response_model=subroutine.views.Collection[subroutine.views.Calendar],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	include_revoked: bool = False,
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Your own feeds, newest first. Never the secret, which cannot be recovered.

	**Yours and nobody else's, including an instance administrator's** — which is where this
	differs from ``GET /v1/tokens``. A list of somebody's feeds says which projects they watch
	and from how many devices, and §20.6 already accepts that a feed URL is a bearer credential
	nobody can audit; an inventory of them is the map that makes one worth stealing.

	Not paginated, for the reason ``GET /v1/users`` gives: how many exist is bounded by how
	many somebody made.
	"""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="calendar"
	)
	found = subroutine.domain.calendars.feeds(
		session, actor.user, include_revoked=include_revoked
	)

	return subroutine.api.shaping.response(
		subroutine.views.calendars(found, session=session, principal=actor),
		subroutine.views.Page(
			limit=len(found), has_more=False, next_cursor=None, total=len(found)
		),
		shape,
	)


@router.post("/{id_or_prefix}/reset", summary="Give a feed a new URL")
def reset (
	id_or_prefix: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
) -> subroutine.views.IssuedCalendar:
	"""Replace a feed's secret, so the URL somebody had stops working immediately.

	**The feed survives and the subscription does not**, which is the point: a leaked URL is
	fixed without losing the scope, the audience, or the record of when it was last polled.
	Revoking and making another would lose all three and hand back a different id.

	Whoever subscribed to the old URL sees their calendar stop updating and is not told why —
	there is nobody to tell. Re-subscribing them is the new URL, given to them the same way.
	"""

	found = subroutine.domain.calendars.mine(session, actor, id_or_prefix)
	minted = subroutine.domain.calendars.reset(session, found)
	rendered = subroutine.views.calendar(
		found,
		url=subroutine.domain.calendars.address(settings.public_url, minted),
		issued=True,
		session=session,
		principal=actor,
	)

	assert isinstance(rendered, subroutine.views.IssuedCalendar)

	return rendered


@router.delete("/{id_or_prefix}", summary="Revoke a calendar feed")
def revoke (
	id_or_prefix: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Calendar:
	"""Stop a feed for good, now.

	Immediate: ``revoked_at`` is read on every poll rather than cached, so there is no window
	to wait out. Whoever holds the URL gets the same 404 as somebody who guessed one.

	Idempotent, and it keeps the first revocation time — when a credential stopped being
	trusted is worth not overwriting. The revoked feed is returned rather than an empty 204, so
	a repeat call is distinguishable from a first one.
	"""

	found = subroutine.domain.calendars.mine(session, actor, id_or_prefix)

	subroutine.domain.calendars.revoke(session, found)

	return subroutine.views.calendar(found, session=session, principal=actor)


@router.get(
	"/{prefix}/{secret}.ics",
	summary="Fetch a calendar feed",
	response_class=starlette.responses.Response,
	responses={404: {"description": "No calendar is at that address."}},
)
def feed (
	prefix: str,
	secret: str,
	request: starlette.requests.Request,
	session: subroutine.api.dependencies.SessionDep,
) -> starlette.responses.Response:
	"""Return one calendar as iCalendar, or 404 if that address names nothing.

	The whole credential is rebuilt from the two path segments rather than either being looked
	up on its own, so this reaches the same `resolve` a caller of the domain would — and the
	grammar that splits a credential into a prefix and a secret stays in one place.
	"""

	settings = getattr(request.app.state, "settings", None)

	# **Refused before anything is read, and with the same 404 as an address naming nothing**
	# (§20.6). An operator who has turned this off has said they do not want feeds served, and
	# a refusal that said *feeds are disabled here* would confirm to whoever holds a leaked URL
	# that it named something real.
	if settings is not None and not settings.calendars_enabled:
		raise _unknown()

	limits = getattr(request.app.state, "limits", None)
	presented = f"{subroutine.auth.TOKEN_SCHEME}_{subroutine.auth.CALENDAR_KIND}_{prefix}_{secret}"

	try:
		found = subroutine.domain.calendars.resolve(session, presented)

	except subroutine.errors.NotFound:
		# **Counted against the address rather than the prefix**, because a prefix that did not
		# resolve is one the caller chose — so keying the counter on it would hand somebody
		# guessing a fresh allowance for every attempt. §7.7's own rule, on a route its
		# dependency does not reach.
		if limits is not None:
			limits.count_a_failure(request)

		raise

	# A poll that found something is counted against the feed, which this program minted.
	if limits is not None:
		limits.count_a_poll(found.token_prefix)

	now = subroutine.db.types.utcnow()
	instance = subroutine.domain.instances.get(session)

	if instance is None:
		# An instance with no identity row cannot mint a stable `UID`, and a feed whose
		# events change identity between polls is worse than one that is not there.
		raise _unknown()

	body = subroutine.domain.icalendar.render(
		subroutine.domain.calendars.occasions(session, found, now=now),
		name=found.title,
		instance_id=instance.id,
		now=now,
		url_for=_addresses(request, session, found),
	)

	tag = _etag(body)

	# **Compared before the body is sent, not before it is built** (§20.5). The tag is over the
	# rendered document, so there is no cheaper thing to compare — and that is the honest
	# trade: what a 304 saves is the transfer and the client's parse, which is most of the cost
	# on a feed somebody polls every fifteen minutes and changes twice a week.
	#
	# **`DTSTAMP` is excluded from the tag**, or every poll would look like a change: it is the
	# moment the document was generated, so a tag over the whole body would differ on every
	# request and a conditional GET would never once succeed.
	if _asked_for(request) == tag:
		return starlette.responses.Response(
			status_code=304, headers={"ETag": tag, "Cache-Control": CACHE_CONTROL}
		)

	return starlette.responses.Response(
		content=body,
		media_type=CONTENT_TYPE,
		headers={"ETag": tag, "Cache-Control": CACHE_CONTROL},
	)


def _etag (body: str) -> str:
	"""Return a validator over what this feed says, ignoring when it was generated.

	**`DTSTAMP` is dropped before hashing**, and that is the whole of why this is a function
	rather than a hash of the body. Every ``VEVENT`` carries the moment the document was
	built, so a tag over the bytes as sent would change on every poll — the revalidation would
	be correct, always miss, and look exactly like one that worked. `#914` is the same defect
	one resource along: a validator that is present and never matches.
	"""

	stable = "".join(
		line for line in body.splitlines(keepends=True) if not line.startswith("DTSTAMP:")
	)

	return f'"{hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]}"'


def _asked_for (request: starlette.requests.Request) -> str | None:
	"""Return the validator this request is asking about, or ``None``.

	``If-None-Match`` may carry a list and may carry ``W/`` weak markers; a client that sends
	either and is answered as though it had sent nothing simply re-downloads, which is a
	correct response and a wasted one.
	"""

	header = request.headers.get("if-none-match")

	if header is None:
		return None

	for candidate in header.split(","):
		cleaned = candidate.strip()

		if cleaned.startswith("W/"):
			cleaned = cleaned[2:]

		if cleaned:
			return cleaned

	return None


def _unknown () -> subroutine.errors.NotFound:
	"""Return this route's one refusal, which every reason gives."""

	return subroutine.errors.NotFound("There is no calendar at that address.")


def _addresses (
	request: starlette.requests.Request,
	session: sqlalchemy.orm.Session,
	found: subroutine.db.models.identity.CalendarFeed,
) -> typing.Callable[[subroutine.db.models.work.Task], str] | None:
	"""Return how to address an item on this instance, or ``None`` if it does not know.

	**An instance that has not been told its own ``public_url`` renders no ``URL`` at all**,
	rather than assembling one from the request's ``Host``. That header is exactly what a
	proxy rewrites, so the guess is wrong on the deployment `docs/hosting.md` recommends — and
	a wrong link in somebody's calendar opens a page that is not theirs, which is worse than
	no link. `#832` is the same reasoning about what an instance may infer about itself.

	**The workspace's slug is resolved once**, not per event: every item a feed shows is in the
	feed's one workspace (the column is `NOT NULL` for this reason among others), so a lookup
	per row would be `#39`'s N+1 for a value that cannot vary.
	"""

	settings = getattr(request.app.state, "settings", None)
	base = None if settings is None else settings.public_url

	if not base:
		return None

	workspace = session.get(
		subroutine.db.models.identity.Workspace, found.workspace_id
	)

	if workspace is None:
		return None

	# `#649`: the path says which rows there are, so an item's address is its workspace and
	# its ref. A project segment is optional there and left out here, because the shortest
	# form is the one that goes on resolving when somebody moves the item.
	prefix = f"{str(base).rstrip('/')}/{workspace.slug}"

	def address (task: subroutine.db.models.work.Task) -> str:
		"""Return where this item lives, as a reader would open it."""

		return f"{prefix}/{task.ref}"

	return address
