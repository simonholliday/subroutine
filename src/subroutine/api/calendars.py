"""The one address a calendar application fetches, and the only public route that reads work.

Everything about the shape is decided in docs/design.md §20 and in decision `#972`; what is here is
HTTP. Four properties of this route are unlike every other one in the application, and each is
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
import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.calendars
import subroutine.domain.icalendar
import subroutine.domain.instances
import subroutine.errors

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
