"""``GET /v1/meta`` — what this installation calls things, and what it will accept.

The endpoint that makes custom vocabulary safe. Statuses, item types and link types are
workspace data (§5.5): an installation renames ``done`` to "Shipped" and adds "Needs
review" freely, so a client that assumes a global vocabulary is a client that breaks on
somebody's second workspace. This publishes the local one instead, and an agent reads it
once rather than discovering it by being refused (SPEC.md §13.1).

**Two rules shape what is in here, and both are about not lying.**

*Nothing is published that is not implemented.* §6.13 already states it for the recurrence
row — "publishing a grammar the installation does not implement is worse than publishing a
smaller one" — and it applies to §9's filter grammar, of which **§9.6's comparison operators
are built and the rest is not** (`#815`). So this reports the query parameters the listings
*actually* accept, read from the application's own OpenAPI document; the dotted filters they
*actually* compile, read from the registry that compiles them; and the sort fields they
*actually* offer, read from the routers' own constants. None can drift, because none is a
second copy.

*Nothing is published at unbounded size.* The tag list is capped and says so. §13.1 exists
to keep this response small, and a workspace with four thousand tags would otherwise make
the discovery endpoint the most expensive call in the API.
"""

import json
import typing
import uuid

import fastapi
import sqlalchemy
import sqlalchemy.orm
import starlette.requests

import subroutine
import subroutine.api.dependencies
import subroutine.api.documents
import subroutine.api.projects
import subroutine.api.query
import subroutine.api.routing
import subroutine.api.security
import subroutine.api.shaping
import subroutine.api.tasks
import subroutine.cli.topics
import subroutine.config
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.filtering
import subroutine.domain.instances
import subroutine.domain.links
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.domain.tasks
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.installations
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1",
	tags=["discovery"],
	route_class=subroutine.api.routing.Transactional,
)

#: How many tags to publish. Ordered by how much they are used, so the cap keeps the ones
#: a client is most likely to need. Appendix A filed this against M3: embedding the whole
#: list makes ``/v1/meta`` exactly the large response §13.1 exists to avoid.
TAG_LIMIT = 50

#: Which ``subroutine explain`` topics the agent guide inlines. **Not all of them**, because
#: §13.3 caps the guide at 8 KB and the topics are what push it over: `scripting` is about a
#: terminal and `refs` says useful things about how a *shell* treats ``#``, neither of which an
#: HTTP client needs. The two that carry grammar it cannot guess — the date vocabulary and the
#: capture line — stay. The rest are one request away at ``/v1/docs/examples`` and in the CLI.
GUIDE_TOPICS = frozenset({"dates", "capture"})

#: The answer to "what is this?" for a caller that has a base URL, a token and nothing else.
#: One sentence about what it is *for that caller*, and one pointer. Deliberately not a
#: description of the data model: an agent can read the model from the rest of this response,
#: and cannot read the reason it should bother from anywhere.
PURPOSE = (
	"Shared project management for people and agents. You are a principal here, not a "
	"tool being driven: what you write is attributed to you, addressable, and still here "
	"after your context is gone. Read GET /v1/docs/agent — it is written for you and says "
	"what that is worth before it says how."
)

#: Query parameters that are not *filters*. Reflection cannot tell the difference by
#: itself — they are all query parameters — and calling `format` a filter would tell an
#: agent it narrows a result set when it changes how the same rows are reported.
NOT_FILTERS = frozenset({"order", "limit", "cursor", "include_total", "format", "fields"})

#: Which listing each entity's filters and sort fields come from. Declared as data so the
#: reflection below has one place to look, and so adding an entity is one line.
LISTINGS: tuple[tuple[str, str, dict[str, typing.Any], frozenset[str]], ...] = (
	("task", "/v1/tasks", subroutine.api.tasks.SORTABLE, subroutine.api.tasks.SELECTABLE),
	(
		"document",
		"/v1/documents",
		subroutine.api.documents.SORTABLE,
		subroutine.api.documents.SELECTABLE,
	),
	(
		"project",
		"/v1/projects",
		subroutine.api.projects.SORTABLE,
		subroutine.api.projects.SELECTABLE,
	),
)


@router.get(
	"/meta",
	summary="What does this installation call things?",
	dependencies=[subroutine.api.query.UnknownQueryDep],
)
def meta (
	request: starlette.requests.Request,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	workspace_id: str | None = fastapi.Query(
		None, description="Which workspace's vocabulary to report, by id or slug."
	),
) -> subroutine.views.Meta:
	"""Report this installation's vocabulary, limits and grammars.

	**Refuses a query parameter it does not accept, which no other single-entity read does**
	(`#615`). The rule `api/query.py` states is that a listing refuses because an ignored
	parameter costs *payload*, and a single-entity read is exempt because it wastes nothing.
	This endpoint breaks that criterion: a discarded parameter here does not return too much,
	it returns **a different answer that looks like a true one**.

	``?workspace=projects`` — the spelling every MCP tool uses — was dropped, and the reply was
	``200`` with ``workspace: null`` and empty vocabulary maps, which is exactly what a fresh
	instance with no custom vocabulary would say. An agent read it that way, concluded the
	status keys were unavailable, derived them instead from the statuses *in use*, decided there
	was no way to close an item as a duplicate, and **deleted a task** rather than cancelling
	it. ``cancelled`` had been there the whole time.

	So the criterion is not "collection or entity", it is whether ignoring the parameter
	changes the *answer* or only its size. This is the one read whose entire answer is chosen
	by a query parameter, with no path segment naming the subject and no ambiguity refusal
	underneath it to catch the mistake.
	"""

	return document(
		session, actor, settings, workspace_id=workspace_id, application=request.app
	)


def document (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	settings: subroutine.config.Settings,
	*,
	workspace_id: str | None,
	application: fastapi.FastAPI,
) -> subroutine.views.Meta:
	"""Assemble what this installation calls things, for either transport — `#486`.

	**Both clients answer this now**, so the assembly is one function rather than an endpoint
	and a local reimplementation. `#483` left `/v1/meta` out of the agent surface precisely
	because a local client would have had to rebuild it, and decision `#484` made that gap
	load-bearing: an agent constructing a raw call needs *this workspace's* status and type
	keys — ``done`` may be called ``Shipped`` here (§5.5) — and nothing else publishes them.

	**It stays inside ``api`` while the models it returns do not**, which is the split worth
	understanding. The models moved to :mod:`subroutine.views` because both clients parse them.
	This does not, because :func:`_listings` reflects the *running application*'s OpenAPI
	document — reporting the HTTP surface is the point, so the assembly belongs with the thing
	it reflects. The local client reaches it through the same documented late import it already
	takes for the reference documents.
	"""

	reachable = subroutine.domain.workspaces.readable(session, actor)

	# Unlike every other endpoint this one does *not* refuse when the workspace is
	# ambiguous: a client's first call is often this one, before it knows what workspaces
	# there are, and answering "which workspace?" to the request that would have told it
	# is a loop. With several and none named, the vocabulary sections are empty and the
	# workspace list is the answer.
	# **An unknown name is a refusal; an *ambiguous* one is not.** The leniency below is about
	# a caller that named nothing, which is right — this is often the first request, before it
	# knows what workspaces exist. A name that matches nothing is a different thing, and
	# answering it with `200` and an empty vocabulary told an agent the workspace had none:
	# "discover by being refused" inverted into "discover by being told something false", from
	# the one endpoint whose purpose is to prevent that. Resolved through the same
	# `domain.selection` every other module uses, so the matching rule cannot drift either.
	chosen = (
		subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
		if workspace_id is not None
		else _sole(reachable)
	)
	instance = subroutine.domain.instances.get(session)

	return subroutine.views.Meta(
		api_version=subroutine.API_VERSION,
		instance_version=subroutine.installations.program(),
		server_time=subroutine.db.types.utcnow(),
		purpose=PURPOSE,
		instance=None if instance is None else subroutine.views.instance(instance),
		source_url=settings.source_url,
		public_url=settings.public_url,
		workspace=None if chosen is None else chosen.id,
		workspaces=[subroutine.views.workspace_ref(workspace) for workspace in reachable],
		statuses={} if chosen is None else _statuses(session, chosen.id),
		item_types={} if chosen is None else _item_types(session, chosen.id),
		link_types=[] if chosen is None else _link_types(session, chosen.id),
		linkable_types=list(subroutine.domain.links.LINKABLE),
		tags=_tags(session, actor, chosen),
		listings=_listings(application),
		grammars=_grammars(),
		limits=subroutine.views.Limits(
			default_page_size=settings.default_page_size,
			max_page_size=settings.max_page_size,
			max_title_length=subroutine.domain.tasks.MAX_TITLE_LENGTH,
			max_hierarchy_depth=settings.max_hierarchy_depth,
			max_estimate_minutes=subroutine.domain.durations.MAX_MINUTES,
		),
		error_codes=sorted(subroutine.errors.REGISTRY),
		docs={
			"agent_guide": "/v1/docs/agent",
			"examples": "/v1/docs/examples",
			"openapi": "/v1/openapi.json",
			"human": "/docs",
		},
	)


def _unbuilt () -> str:
	"""Name the unbuilt features in a sentence, from the list a test can check.

	Written out here rather than typed into the prose so that the sentence and the guard read
	the same tuple — a list somebody keeps in their head is the thing `#355` was.
	"""

	named = [name for name, _fragment in UNBUILT]

	if len(named) == 1:
		return named[0]

	return f"{', '.join(named[:-1])}, and {named[-1]}"


@router.get("/docs/agent", summary="A guide written for an agent", response_class=fastapi.responses.PlainTextResponse)
def agent_guide (actor: subroutine.api.security.PrincipalDep) -> str:
	"""Serve §13.3's guide. The text is :func:`guide_text`, so a client can reach it too."""

	return guide_text()


def guide_text () -> str:
	"""Return the agent guide as Markdown.

	Authenticated, though it discloses nothing about the installation: §8.6 marks only
	``/healthz`` and ``/readyz`` as unauthenticated, and a reader who has no token has
	nothing to use the guide for. Caught by ``tests/test_api_authentication.py`` rather
	than by anybody noticing, which is the point of that test.

	The same text the CLI prints for ``subroutine explain <topic>``, which is itself generated
	from the parsers rather than transcribed (S2-06): the date vocabulary comes from
	``dates.KEYWORDS``, the capture table from ``capture``'s own constants, the estimate
	units from ``durations.UNITS``. A guide listing a keyword the parser rejects is worse
	than no guide, and one written twice becomes that within a release.

	SPEC.md §13.3 also asks for worked request/response examples executed by a CI job so they
	cannot drift. Those are at ``/v1/docs/examples`` and ``tests/test_api_examples.py`` runs
	every one of them against a real instance, so an example that stops working fails the
	build. **This paragraph said they were still owed until `#315`** — five lines above the
	text below, which tells the reader to go and read them.

	**It opens with what the reader gets, not with how to authenticate.** An agent arriving
	with a base URL and a token has no other way to learn why it should bother, and a guide
	that begins "read /v1/meta first" reads as a chore having been handed over. §14.1 already
	enumerates the failure modes this is for; that list had simply never been pointed at the
	reader it describes.

	**And it describes only what is built.** §13.1's rule — publishing what an installation
	does not implement is worse than publishing less — matters more here than anywhere: an
	agent told to leave a handoff at an endpoint that 404s trusts nothing else in the
	document. Sessions, decisions and verification evidence are M4-M7 and named as unbuilt
	rather than promised.

	**The inverse cost the same and nobody was watching for it** (`#355`). Claims shipped, and
	this list went on naming them unbuilt — to the one reader with no alternative, since an
	agent on MCP has the skill and a person has ``--help``, and an agent arriving here with a
	base URL and a token has only this. The endpoint check compares the paths this document
	*names* against routes that exist; it cannot see a route named nowhere. So a capability
	added anywhere near this list is owed a look at it.
	"""

	sections = [
		"# Subroutine — a guide for agents",
		"",
		"You are a principal here, not a tool being driven. You have an identity, your actions "
		"are attributed to you, and what you write is addressable by everyone else working on "
		"the same thing. Neither kind of user is a guest in the other's system.",
		"",
		"## Why this is worth your context",
		"",
		"Four things reliably go wrong across sessions. Each has somewhere to put the answer, "
		"and the reason to write it there is that *you* get it back:",
		"",
		"| What goes wrong | Where it goes | What you get back |",
		"| --- | --- | --- |",
		"| Your context ends, and what is not written down is gone | A document, linked to "
		"the task it came from | The next session opens with your conclusion rather than "
		"re-deriving it |",
		"| You re-propose an approach already ruled out | A document recording the decision "
		"*and* what was rejected | You find it before proposing it again — the failure a human "
		"finds most irritating, and the cheapest to prevent |",
		"| You re-read the same files to rebuild the same understanding | A document, with "
		"`#42` in its body to tie it to the work | The mention is indexed, so the next "
		"session finds it from either end |",
		"| You cannot tell what moved while you were away | `GET /v1/changes?since=` | You "
		"resume from where you stopped instead of re-reading the backlog or trusting a "
		"snapshot that has quietly gone stale |",
		"| Somebody edits the same item while you are thinking | `expected_version` on every "
		"write | A `409` telling you to re-read, rather than a silent overwrite of somebody's "
		"edit |",
		"| Another worker starts the task you just started | `POST /v1/tasks/{ref}/claim`, "
		"then `/release` at the end | It leaves their ready listing while you hold it — a "
		"lease, so nothing is stranded if you stop first. Finishing does **not** release it |",
		"| Nobody can see that you are working | `status_category` `in_progress`, set when "
		"you begin | A person watching sees the work move, rather than items appearing "
		"finished with nothing in between |",
		"",
		"**Being bounded is what earns you more to do.** Your token can be scoped narrower "
		"than its owner's — particular permissions, particular projects, one workspace. That "
		"is the reason a human hands over work they would otherwise supervise. `GET /v1/me` "
		"says exactly what you may do, so you never learn it by being refused.",
		"",
		f"**Not built yet, and named rather than promised:** {_unbuilt()}. Specified in full, "
		"not here. What is above is what exists.",
		"",
		"## How to use it",
		"",
		"**`GET /v1/docs/examples` has a worked request and response for each of these.** Read "
		"it once; it is the fastest way to stop guessing at shapes.",
		"",
		"The four endpoints that do most of the work: `GET /v1/agenda` answers \"what should I "
		"do today\" across every workspace at once — add `?workspace_id=` (an id or a short "
		"name from the `workspaces` list above) to narrow it to one, which is what you want "
		"when a personal list and a project's backlog share an instance; `GET /v1/tasks` "
		"lists one workspace's; "
		"`POST /v1/tasks` creates one, from `title` or from a `text` line; and "
		"`POST /v1/documents` is where a conclusion goes, tied to the task it came from with "
		"`POST /v1/tasks/{ref}/links`.",
		"",
		# The feed is the endpoint an agent most needs and was the one the guide did not
		# mention (`#313`) — built for exactly this reader and invisible to it for a release.
		# Placed with the four above rather than in a section of its own, because it is the
		# call to make *first* in a session and a heading further down would be read last.
		"**Start a session with `GET /v1/changes`**, which answers what has moved since you "
		"last looked. Keep the `seq` of the last event you dealt with and send it back as "
		"`?since=`; it is inclusive, so you see that one again and skip what you already have. "
		"`?actor=me` narrows it to what your own credential did, which is how you pick your "
		"own unfinished work out of everybody's. Without this a context window is a snapshot "
		"that does not decay: nothing tells you a thing you read on Tuesday is now closed, so "
		"you go on reporting it open, confidently. The feed deliberately withholds the last "
		"second, so an event you have just written may take a moment to appear.",
		"",
		"Read `GET /v1/meta` first: it reports this installation's statuses, item types, "
		"link types and limits, which are workspace data and are not the same everywhere.",
		"",
		"Authenticate with `Authorization: Bearer sr_…`. `GET /v1/me` reports who you are "
		"and exactly what you may do, already narrowed by your token — you never need to "
		"work that out by being refused.",
		"",
		# **Below the two `/v1/me` paragraphs and not between them.** "It also reports" names
		# that endpoint, so a paragraph inserted above it silently changes what "it" is — found
		# by reading the rendered guide rather than the source, which is the only place the
		# join between two paragraphs exists at all.
		"It also reports `instance_version` and `schema_revision`: what this installation "
		"runs, and which migration its database is at. Read them when a field you expected "
		"is absent — a client ahead of its instance is ordinary, and looks from your side "
		"exactly like a feature that was never built.",
		"",
		# `#780`. Served since `#516` and named by neither channel a reader is guaranteed, so
		# an agent arriving with an address and a token could not learn that its own protocol
		# was already answering at that address. Decision `#499` is the rule, and this is the
		# one document its reader has. `tests/test_api_meta.py` derives the claim from the
		# mounted routes rather than trusting this paragraph to keep in step.
		"**This instance speaks MCP as well, at `POST /mcp`** — the same bearer credential, "
		"and `?workspace=` chooses a default workspace. If your client speaks MCP, that is "
		"one address and one token with nothing installed at your end, and the tools you get "
		"are this instance's own, so they can never be older than it. They are a deliberately "
		"small, opinionated surface over the same data; `subroutine_call_api` reaches the "
		"rest of what is described below.",
		"",
		"On `PATCH`, a field you omit is left alone and a field you send as `null` is "
		"cleared — the only way to clear a date. **The names you write are not the names you "
		"read:** send `due` and `start`, which accept the whole date grammar; you get back "
		"`due_at` and `start_at`, which are instants. Sending `due_at` is a 422, because an "
		"unknown field is refused rather than ignored.",
		"",
		# The `refs` topic below is written for somebody at a terminal, and says useful
		# things an HTTP client does not need — how a shell treats `#`. These are the facts
		# it does not cover, and they belong here rather than there: sharing one text with
		# `subroutine explain` is deliberate (§12.2a), and the way to keep that honest is to put
		# audience-specific detail in the audience's own preamble.
		"**An item's `ref` is an integer, and it is how you address one.** "
		"`GET /v1/tasks/42` and `GET /v1/tasks/{id}` are the same request; every "
		"task- and document-addressed endpoint takes either. Refs are unique per workspace "
		"and shared between tasks and documents, they are never reused, and they never "
		"change — not when an item moves between projects. In a request body, a field that "
		"names another item (`target`, `supersedes`, `parent`) takes the same integer, so "
		"you can send back what you were given without converting it.",
		"",
		# The ordering here is the recommendation, and it is deliberate: `fields` first
		# because it is the only economy that loses nothing. An earlier draft led with
		# `format=compact` on the strength of its size alone, which was wrong twice over —
		# it is *larger* than a two-field selection on the same page, and it truncates.
		"**Ask for less.** A full task is 400-600 tokens, mostly fields you did not need. "
		"`?fields=ref,title,due_at` returns only those: lossless, ~20x smaller, the one to "
		"reach for. `?format=ids` gives addresses alone, ~200x smaller, for choosing what to "
		"look at next. `?format=compact` is a *terminal* rendering — aligned columns, long "
		"titles cut short; read it, do not parse it. The `items`/`page` envelope is the same "
		"in all three, so pagination does not change. `fields` and `format` cannot be "
		"combined. `GET /v1/meta` lists the selectable fields and formats per entity.",
		"",
		# `#815`, and it is in the guaranteed channel by decision `#499`: an agent asked *what
		# was created yesterday* has no other way to learn that the question is expressible.
		# The names are not spelled out here because `/v1/meta`'s `listings` publishes them from
		# the registry that enforces them — a second copy in prose is what goes stale.
		"**Ask a listing about a date**: `?<field>.<operator>=<when>`, where the operator is "
		f"one of {_operators()} and `<when>` is a day, an instant or any expression "
		"from the date grammar below. `GET /v1/tasks?created_at.gte=yesterday` and "
		"`?completed_at.gte=start_of_week` answer *what happened recently*; two of them make a "
		"window. Days are read in your timezone and a bound takes in the whole day it names, so "
		"`created_at.lte=yesterday` includes all of yesterday. Combine them freely with "
		"`project`, `assignee`, `q` and the rest. `GET /v1/meta` lists which fields each "
		"listing accepts; equality is refused on a timestamp, because it would compare against "
		"one microsecond and match nothing.",
		"",
		# `#815`'s third and fourth questions. Named separately from the paragraph above,
		# because it is the one filter that is not a column — an agent reading it as "another
		# date field" reaches for `updated_at` and is told, wrongly, that nothing happened.
		"**`touched_at` is the one to reach for when you mean *worked on*.** "
		"`?touched_at.gte=yesterday` finds what was created, edited, completed, commented on "
		"or moved through a status — **including changes that move no field on the item "
		"itself**, which is exactly why `updated_at` is a different question: writing a comment "
		"does not touch the commented-on item's `updated_at` at all. `?touched_by.eq=<username>` "
		"narrows it to one person, and the two are one question rather than two, so they match "
		"the same events. Claiming and releasing do not count — that is bookkeeping, not work "
		"— and work you finished in the period is included, since finishing something is the "
		"clearest case of having worked on it. `include_completed=false` beside it narrows to "
		"what is still in flight.",
		"",
		# "a comment" was removed from this list while comments had no API, per the rule in
		# this function's docstring — a reader told that references work in comments would
		# have gone looking for an endpoint that was not there. It is back because the
		# endpoint is: `POST /v1/tasks/{ref}/comments`, and the same on projects and
		# documents.
		"In prose — a title, a description, a document body, a comment — a reference is "
		"written `#42`, and that is what builds the mention index. The sigil belongs to the "
		"*text*: do not put it in a URL, where it would have to be escaped, and do not "
		"expect it in the `ref` field, which is a number.",
		"",
		"**Record what happened as you go:** `POST /v1/tasks/{ref}/comments` with a `body`, "
		"and the same on projects and documents. A comment is what happened; a document is "
		"what you concluded.",
		"",
		"**If you read something, think, and then write it, send the version back.** Put "
		"`expected_version` in the body or `If-Match: \"<version>\"` in the header, and a "
		"change made by somebody else in between is refused with a `409` carrying both "
		"version numbers — rather than silently overwriting their work. A human may be "
		"editing the same task in a text editor while you think.",
		"",
	]

	for topic in subroutine.cli.topics.TOPICS:
		if topic.name not in GUIDE_TOPICS:
			continue

		sections.append(f"## {topic.name.capitalize()}")
		sections.append("")
		sections.append(topic.summary)
		sections.append("")
		sections.append(topic.body)
		sections.append("")

	return "\n".join(sections)


def _operators () -> str:
	"""Name the comparisons a date filter takes, read from the kind that enforces them.

	**Not written out in the guide**, because a list in prose is a second copy of a rule and
	this one has already moved once: `eq` and `ne` were accepted when the compiler was written
	and refused a day later (`#817`). A guide naming an operator the listing declines is worse
	than one naming fewer.
	"""

	return ", ".join(
		f"`{operator}`"
		for operator in sorted(subroutine.domain.filtering.INSTANT.operators)
	)


def _sole (reachable: typing.Sequence[typing.Any]) -> typing.Any:
	"""Return the only workspace there is, or ``None`` when the caller must choose.

	**The one place in the API that does not refuse an ambiguous workspace**, and deliberately:
	a client's first call is often this one, before it knows what workspaces there are, so
	answering "which workspace?" to the request that would have told it is a loop. With several
	and none named, the vocabulary sections are empty and the ``workspaces`` list is the answer.

	Naming one that does not exist is *not* this case and is refused by the caller, through
	``domain.selection`` like everything else.
	"""

	return reachable[0] if len(reachable) == 1 else None


def _statuses (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID
) -> dict[str, list[subroutine.views.Status]]:
	"""Return every status this workspace has, grouped by what it applies to."""

	model = subroutine.db.models.vocabulary.Status
	grouped: dict[str, list[subroutine.views.Status]] = {}

	for row in session.scalars(
		sqlalchemy.select(model)
		.where(model.workspace_id == workspace_id)
		.order_by(model.entity_type, model.position)
	):
		grouped.setdefault(row.entity_type, []).append(
			subroutine.views.Status(
				key=row.key, label=row.label, category=row.category, is_default=row.is_default
			)
		)

	return grouped


def _item_types (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID
) -> dict[str, list[subroutine.views.Named]]:
	"""Return every item type this workspace has, grouped by what it applies to."""

	model = subroutine.db.models.vocabulary.ItemType
	grouped: dict[str, list[subroutine.views.Named]] = {}

	for row in session.scalars(
		sqlalchemy.select(model)
		.where(model.workspace_id == workspace_id)
		.order_by(model.entity_type, model.position)
	):
		grouped.setdefault(row.entity_type, []).append(
			subroutine.views.Named(key=row.key, label=row.label, is_default=row.is_default)
		)

	return grouped


def _link_types (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID
) -> list[subroutine.views.LinkType]:
	"""Return every link type this workspace has."""

	model = subroutine.db.models.vocabulary.LinkType

	return [
		subroutine.views.LinkType(
			key=row.key,
			title=row.title,
			inverse_title=row.inverse_title,
			is_symmetric=row.is_symmetric,
		)
		for row in session.scalars(
			sqlalchemy.select(model).where(model.workspace_id == workspace_id).order_by(model.key)
		)
	]


def _tags (
	session: sqlalchemy.orm.Session,
	actor: typing.Any,
	workspace: typing.Any,
) -> subroutine.views.Tags:
	"""Return the most-used tags, capped, and say how many were left out.

	Usage is counted over the tasks this caller can actually see, so a tag used only in a
	private project they are not a member of does not appear — a tag list is a small
	disclosure, but it is one, and there is no reason for it to be the exception.
	"""

	if workspace is None:
		return subroutine.views.Tags(items=[], total=0, truncated=False)

	tag = subroutine.db.models.vocabulary.Tag
	joined = subroutine.db.models.work.TaskTag
	visible = subroutine.domain.scoping.readable_tasks(
		actor, workspace_ids=[workspace.id]
	).subquery()

	usage = (
		sqlalchemy.select(tag.name, sqlalchemy.func.count(joined.task_id).label("usage"))
		.select_from(tag)
		.outerjoin(joined, joined.tag_id == tag.id)
		.outerjoin(visible, visible.c.id == joined.task_id)
		.where(tag.workspace_id == workspace.id, joined.task_id.is_(None) | visible.c.id.isnot(None))
		.group_by(tag.id, tag.name)
		.order_by(sqlalchemy.desc("usage"), tag.name)
	)

	rows = session.execute(usage.limit(TAG_LIMIT + 1)).all()
	total = session.scalar(
		sqlalchemy.select(sqlalchemy.func.count()).select_from(
			sqlalchemy.select(tag.id).where(tag.workspace_id == workspace.id).subquery()
		)
	)

	return subroutine.views.Tags(
		items=[subroutine.views.Tag(name=row[0], usage=row[1]) for row in rows[:TAG_LIMIT]],
		total=total or 0,
		truncated=len(rows) > TAG_LIMIT,
	)


def _listings (application: fastapi.FastAPI) -> dict[str, subroutine.views.Listing]:
	"""Report what each collection endpoint accepts, read from the application itself.

	The filters come out of the generated OpenAPI document and the sort fields out of the
	routers' own ``SORTABLE`` constants, so both are the live values rather than a
	description of them.

	**§9.6's dotted filters are added from the registry that enforces them** (`#815`), because
	they are not OpenAPI parameters — one name stands for a whole family and FastAPI has no way
	to declare that. Read from :func:`subroutine.domain.filtering.names` rather than described
	here, so this cannot advertise a combination the listing refuses. The rest of §9 — a JSON
	body, boolean composition, string and collection operators — is specified and not built, and
	is deliberately still absent: publishing it would be publishing a language this installation
	does not speak.
	"""

	schema = application.openapi()
	found: dict[str, subroutine.views.Listing] = {}

	for entity, path, sortable, selectable in LISTINGS:
		operation = schema.get("paths", {}).get(path, {}).get("get", {})
		parameters = [
			parameter["name"]
			for parameter in operation.get("parameters", [])
			if parameter.get("in") == "query"
			and parameter["name"] not in NOT_FILTERS
		]

		found[entity] = subroutine.views.Listing(
			path=path,
			filters=sorted(parameters + list(subroutine.domain.filtering.names(entity))),
			sortable=sorted(sortable),
			selectable=sorted(selectable),
			formats=list(subroutine.api.shaping.FORMATS),
		)

	return found


def _grammars () -> dict[str, subroutine.views.Grammar]:
	"""Report the small closed languages, read from the parsers that enforce them."""

	return {
		"relative_dates": subroutine.views.Grammar(
			description=(
				"A keyword, optionally shifted: <keyword>[+-]<n><unit>. Units are "
				"minutes, hours, days, weeks, months (capital M) and years."
			),
			vocabulary=list(subroutine.domain.dates.KEYWORDS),
			examples=["today", "now+90m", "end_of_week", "start_of_month+1M"],
		),
		"durations": subroutine.views.Grammar(
			description=(
				"A number and a unit, largest first, each unit at most once. A unit is "
				"always required."
			),
			vocabulary=[unit for unit, _minutes in subroutine.domain.durations.UNITS],
			examples=["90m", "1h30m", "2d", "1w2d"],
		),
		"capture": subroutine.views.Grammar(
			description=(
				"One line, parsed into fields. Anything not recognised stays in the title, "
				"verbatim."
			),
			vocabulary=[
				"#tag",
				"@assignee",
				"!importance (1-5), or !importance/urgency for both of §6.3's axes",
				"~estimate",
				"+project",
				*(f"{word} <date>" for word in sorted(subroutine.domain.capture.PLANNED_WORDS)),
				*(f"{word} <date>" for word in sorted(subroutine.domain.capture.DEADLINE_WORDS)),
			],
			examples=[
				"Renew the domain by friday !4",
				"Fix the header +web #bug ~2h @alice",
				"Call the dentist tomorrow",
			],
		),
	}

#: Worked calls, in the order an agent actually needs them. §13.3 has asked for these since
#: S3-05 and they did not fit: the guide is capped at 8 KB and orientation had to win. Their own
#: path is the honest answer — it also means an example can be long enough to be useful, and
#: that the guide's promises ("a document, linked to the task it came from") have somewhere to
#: point instead of being unactionable.
#:
#: What §14 and §15 specify and this build does not implement, each beside the path segment
#: its endpoints would carry if it did.
#:
#: **The fragment is what makes the claim checkable, and is the whole point of this being data**
#: (`#355`). §13.1's rule is that promising what an installation does not implement is worse
#: than publishing less, and the guide has always honoured it — but the *inverse* cost exactly
#: the same and nothing was watching for it: claims shipped and this list went on calling them
#: unbuilt, to the one reader with no other source. ``tests/test_api_meta.py`` now fails if any
#: route this application serves contains one of these fragments, so building a thing is what
#: deletes its entry.
#:
#: The guide's own endpoint check cannot do this. It compares the paths the document *names*
#: against routes that exist, which proves every path named is real and says nothing at all
#: about a real path named nowhere.
UNBUILT: tuple[tuple[str, str], ...] = (
	("session handoffs", "/sessions"),
	("decisions as a first-class entity", "/decisions"),
	("verification evidence on a completion", "/verifications"),
)

#: Every ``EXAMPLES`` entry is executed by ``tests/test_api_examples.py`` against a real
#: application, so a shape here cannot drift from the endpoint. That is the whole reason they
#: are data rather than prose.
EXAMPLES: tuple[tuple[str, str, str, dict[str, typing.Any] | None], ...] = (
	(
		"What should I work on today, across every workspace at once?",
		"GET",
		"/v1/agenda",
		None,
	),
	# These two used to be one example whose description talked about `format=ids` while the
	# request it ran was `format=compact`. Executing an example proves it *works*, not that
	# what is said about it is true — `test_api_examples` now checks that too.
	(
		"List open tasks cheaply, losing nothing. `fields=` is the economy to reach for: "
		"~20x smaller than full, and still structured.",
		"GET",
		"/v1/tasks?fields=ref,title,due_at,priority_score&limit=5",
		None,
	),
	(
		"Decide what to look at next. `format=ids` is ~200x smaller than full — addresses "
		"alone, then fetch the few you actually want.",
		"GET",
		"/v1/tasks?format=ids&limit=5",
		None,
	),
	# `#815`. Two examples rather than one, because the pair is the point: the first is the
	# question Simon actually asked, and the second is the answer to "can I combine it with the
	# filters I already use", which is the reason this is a filter and not a second endpoint.
	(
		"What was worked on recently? A date field takes `.gte`, `.gt`, `.lt` and `.lte`, and "
		"the value is the same date grammar a write accepts — so `yesterday` and "
		"`start_of_week` work, read in your timezone.",
		"GET",
		"/v1/tasks?updated_at.gte=start_of_week&fields=ref,title,updated_at&limit=5",
		None,
	),
	(
		"What was created in a window, in one project? Two bounds make a range, and a date "
		"filter narrows alongside every other one rather than replacing it.",
		"GET",
		"/v1/tasks?created_at.gte=now-30d&created_at.lt=today&format=ids&limit=5",
		None,
	),
	(
		"What was *worked on* recently? `touched_at` reads the event feed rather than the "
		"row, so a comment or a status change counts — neither of which moves `updated_at` "
		"on the item itself. Add `touched_by.eq=<username>` for one person's.",
		"GET",
		"/v1/tasks?touched_at.gte=now-7d&fields=ref,title&limit=5",
		None,
	),
	(
		"What blocks what, in one request. `include=links` returns the links among the "
		"page's items beside them, so a dependency graph costs one call rather than one "
		"per item.",
		"GET",
		"/v1/tasks?include=links&fields=ref,title,priority_score&limit=5",
		None,
	),
	(
		"Create a task from a line of text — dates, tags, priority and estimate are parsed "
		"out of it (see the Capture section of /v1/docs/agent).",
		"POST",
		"/v1/tasks",
		{"text": "Research audio devices for 4.0 output on Windows !3 ~2h #audio"},
	),
	(
		"Write down what you concluded. This is where a finding belongs — a comment is what "
		"happened, a document is what you concluded.",
		"POST",
		"/v1/documents",
		{
			"title": "Audio device options for 4.0 on Windows",
			"body": "WASAPI exclusive mode gives 4.0. Realtek drivers refuse it; see #1.",
			"type": "note",
		},
	),
	(
		"Tie the document to the task it came from. **`target_type` defaults to `task`**, so "
		"linking to a document without it is a 404 about a task that does not exist — refs "
		"are shared between tasks and documents, so this is the easiest mistake to make.",
		"POST",
		"/v1/tasks/1/links",
		{"target": 2, "target_type": "document", "link_type": "derives_from"},
	),
	(
		"Set the day you will do something. Send `planned_for`, `due` or `start` — the names "
		"you *write*. You read back `due_at` and `start_at`.",
		"PATCH",
		"/v1/tasks/1",
		{"planned_for": "tomorrow"},
	),
	(
		"Clear a date: send it as null. Omitting it would leave it alone (§8.3).",
		"PATCH",
		"/v1/tasks/1",
		{"due": None},
	),
	(
		"Read what has happened to one item — newest first, and including a change made a "
		"moment ago. Comments made on it are here too, as events whose `subject_id` is this "
		"item and whose `entity_id` is the comment. This is the *history* of one thing; "
		"`/v1/changes` below is the feed of everything.",
		"GET",
		"/v1/tasks/1/events",
		None,
	),
	(
		"Ask what has changed across everything you can see — the question to open a session "
		"with, when your last one ended and you do not know what moved. Oldest first. Keep the "
		"`seq` of the last event you dealt with and send it back as `?since=` next time; it is "
		"inclusive, so you will see that one again and should ignore what you already have. "
		"Add `?actor=me` for what this credential itself did. Events under a second old are "
		"withheld on purpose, so that nothing can be committed behind a cursor you have "
		"already advanced past.",
		"GET",
		"/v1/changes",
		None,
	),
	(
		"Take a task, so another worker does not start it too. `?ready=true` listings hide "
		"what somebody else holds and never hide your own. It is a **lease**: it expires by "
		"itself, so send this again while you are still working, and nothing is stranded if "
		"your session ends first. `POST /v1/tasks/1/release` gives it back. Claiming what "
		"somebody else holds is a `409` naming who and until when.",
		"POST",
		"/v1/tasks/1/claim",
		None,
	),
	(
		"Finish it, without needing to know what this installation calls 'done'.",
		"POST",
		"/v1/tasks/1/complete",
		None,
	),
)


@router.get(
	"/docs/examples",
	summary="Worked calls, in the order an agent needs them",
	response_class=fastapi.responses.PlainTextResponse,
)
def agent_examples (actor: subroutine.api.security.PrincipalDep) -> str:
	"""Serve the worked calls. The text is :func:`examples_text`, so a client can reach it."""

	return examples_text()


def examples_text () -> str:
	"""Return a worked request for each thing an agent most often needs to do.

	Requests only, deliberately. A recorded *response* is a second copy of the view models and
	would drift the moment a field is added — whereas a request is short, and the endpoint
	itself is the authority on what comes back. ``tests/test_api_examples.py`` executes every
	one of these against a real application, so an example that stopped working fails the build.
	"""

	lines = [
		"# Subroutine — worked examples",
		"",
		"Authenticate every one of these with `Authorization: Bearer sr_…`. Read",
		"`GET /v1/docs/agent` first for what these are *for*; this file is the shapes.",
		"",
		"A `ref` is the small integer in every response. It is unique per workspace, shared",
		"between tasks and documents, and never reused — so `/v1/tasks/1` and `/v1/tasks/{id}`",
		"are the same request.",
		"",
	]

	for description, method, path, body in EXAMPLES:
		lines.append(f"## {description}")
		lines.append("")
		lines.append("```http")
		lines.append(f"{method} {path}")

		if body is not None:
			lines.append("Content-Type: application/json")
			lines.append("")
			lines.append(json.dumps(body, indent=2))

		lines.append("```")
		lines.append("")

	return "\n".join(lines)
