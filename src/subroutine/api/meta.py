"""``GET /v1/meta`` — what this installation calls things, and what it will accept.

The endpoint that makes custom vocabulary safe. Statuses, item types and link types are
workspace data (§5.5): an installation renames ``done`` to "Shipped" and adds "Needs
review" freely, so a client that assumes a global vocabulary is a client that breaks on
somebody's second workspace. This publishes the local one instead, and an agent reads it
once rather than discovering it by being refused (SPEC.md §13.1).

**Two rules shape what is in here, and both are about not lying.**

*Nothing is published that is not implemented.* §6.13 already states it for the recurrence
row — "publishing a grammar the installation does not implement is worse than publishing a
smaller one" — and it applies to the §9 filter operators too, which are specified and not
built. So this reports the query parameters the listings *actually* accept, read from the
application's own OpenAPI document, and the sort fields they *actually* offer, read from
the routers' own constants. Neither can drift, because neither is a second copy.

*Nothing is published at unbounded size.* The tag list is capped and says so. §13.1 exists
to keep this response small, and a workspace with four thousand tags would otherwise make
the discovery endpoint the most expensive call in the API.
"""

import datetime
import typing
import uuid

import fastapi
import pydantic
import sqlalchemy
import sqlalchemy.orm
import starlette.requests

import subroutine
import subroutine.api.dependencies
import subroutine.api.documents
import subroutine.api.projects
import subroutine.api.security
import subroutine.api.shaping
import subroutine.api.tasks
import subroutine.cli.topics
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.instances
import subroutine.domain.links
import subroutine.domain.scoping
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(prefix="/v1", tags=["discovery"])

#: How many tags to publish. Ordered by how much they are used, so the cap keeps the ones
#: a client is most likely to need. Appendix A filed this against M3: embedding the whole
#: list makes ``/v1/meta`` exactly the large response §13.1 exists to avoid.
TAG_LIMIT = 50

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


class Named(pydantic.BaseModel):
	"""A vocabulary entry, as this workspace has it.

	``label`` rather than ``title``, matching both the column and §13.2's example: it is
	what to show a person, while ``key`` is what to send back.
	"""

	key: str
	label: str
	is_default: bool = False


class Status(Named):
	"""A status, with the fixed category a client may branch on."""

	#: The key is renameable; the category is not. Branch on this.
	category: str


class LinkType(pydantic.BaseModel):
	"""A link type, and how it reads from each end.

	There is no ``inverse_key``, and that is settled rather than missing: **the API names
	the direction, not the inverse type**. A link response carries ``link_type`` (this key),
	``direction`` (``outgoing`` or ``incoming``) and a ``label`` already the right way round.
	Deriving an inverse key by lower-casing ``inverse_title`` works for the five seeded
	types and breaks on the first custom one.
	"""

	key: str
	title: str
	inverse_title: str
	is_symmetric: bool


class Tag(pydantic.BaseModel):
	"""A tag, and how much it is used."""

	name: str
	usage: int


class Tags(pydantic.BaseModel):
	"""The tag list, and an honest statement of what was left out."""

	items: list[Tag]
	total: int
	truncated: bool


class Listing(pydantic.BaseModel):
	"""What one collection endpoint accepts.

	Reflected from the running application, so it cannot claim a filter that does not
	exist or omit one that does.
	"""

	path: str
	filters: list[str]
	sortable: list[str]

	#: What ``?fields=`` may name, and what ``?format=`` accepts (SPEC.md §14.10). Published
	#: for the reason ``sortable`` is: an agent that has to discover a field name by being
	#: refused has paid for the discovery in context, which is the cost shaping exists to
	#: avoid in the first place.
	selectable: list[str]
	formats: list[str]


class Grammar(pydantic.BaseModel):
	"""One of the small closed languages this installation parses."""

	description: str
	vocabulary: list[str]
	examples: list[str]


class Limits(pydantic.BaseModel):
	"""The bounds a request is held to."""

	default_page_size: int
	max_page_size: int
	max_title_length: int
	max_hierarchy_depth: int
	max_estimate_minutes: int


class Meta(pydantic.BaseModel):
	"""Everything needed to construct a valid request against *this* installation."""

	api_version: str
	server_time: datetime.datetime
	instance: subroutine.views.Instance | None

	#: Where this instance's source can be obtained. Published because the AGPL's network
	#: clause requires a served instance to offer its source to the people using it
	#: (SPEC.md §2.2), which makes this a product requirement rather than a footnote.
	source_url: str

	#: The address this instance is served on, when a deployment has said (SPEC.md §12.4).
	#: Null on a laptop listening on loopback, which is the ordinary case and is not a gap: a
	#: client that reached this response already knows one address that works. It is here for
	#: the client that must hand out a *durable* one — a webhook target, a shared link, or the
	#: ``subroutine:`` address of an item on this instance — which is not the same as whatever
	#: host happened to be dialled.
	public_url: str | None

	workspace: uuid.UUID | None
	workspaces: list[subroutine.views.WorkspaceRef]

	statuses: dict[str, list[Status]]
	item_types: dict[str, list[Named]]
	link_types: list[LinkType]
	linkable_types: list[str]
	tags: Tags

	listings: dict[str, Listing]
	grammars: dict[str, Grammar]
	limits: Limits
	error_codes: list[str]
	docs: dict[str, str]


@router.get("/meta", summary="What does this installation call things?")
def meta (
	request: starlette.requests.Request,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	workspace_id: str | None = fastapi.Query(
		None, description="Which workspace's vocabulary to report, by id or slug."
	),
) -> Meta:
	"""Report this installation's vocabulary, limits and grammars."""

	reachable = subroutine.domain.workspaces.readable(session, actor)

	# Unlike every other endpoint this one does *not* refuse when the workspace is
	# ambiguous: a client's first call is often this one, before it knows what workspaces
	# there are, and answering "which workspace?" to the request that would have told it
	# is a loop. With several and none named, the vocabulary sections are empty and the
	# workspace list is the answer.
	chosen = _chosen(reachable, workspace_id)
	instance = subroutine.domain.instances.get(session)

	return Meta(
		api_version=subroutine.API_VERSION,
		server_time=subroutine.db.types.utcnow(),
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
		listings=_listings(request),
		grammars=_grammars(),
		limits=Limits(
			default_page_size=settings.default_page_size,
			max_page_size=settings.max_page_size,
			max_title_length=subroutine.domain.tasks.MAX_TITLE_LENGTH,
			max_hierarchy_depth=settings.max_hierarchy_depth,
			max_estimate_minutes=subroutine.domain.durations.MAX_MINUTES,
		),
		error_codes=sorted(subroutine.errors.REGISTRY),
		docs={
			"agent_guide": "/v1/docs/agent",
			"openapi": "/v1/openapi.json",
			"human": "/docs",
		},
	)


@router.get("/docs/agent", summary="A guide written for an agent", response_class=fastapi.responses.PlainTextResponse)
def agent_guide (actor: subroutine.api.security.PrincipalDep) -> str:
	"""Return the agent guide as Markdown.

	Authenticated, though it discloses nothing about the installation: §8.6 marks only
	``/healthz`` and ``/readyz`` as unauthenticated, and a reader who has no token has
	nothing to use the guide for. Caught by ``tests/test_api_authentication.py`` rather
	than by anybody noticing, which is the point of that test.

	The same text the CLI prints for ``subroutine help <topic>``, which is itself generated
	from the parsers rather than transcribed (S2-06): the date vocabulary comes from
	``dates.KEYWORDS``, the capture table from ``capture``'s own constants, the estimate
	units from ``durations.UNITS``. A guide listing a keyword the parser rejects is worse
	than no guide, and one written twice becomes that within a release.

	SPEC.md §13.3 asks for more than this — ten worked request/response examples, executed
	by a CI job so they cannot drift. That is still owed and is filed in Appendix A. What is
	here is the half that already exists and is already drift-proof.
	"""

	sections = [
		"# Subroutine — a guide for agents",
		"",
		"Read `GET /v1/meta` first: it reports this installation's statuses, item types, "
		"link types and limits, which are workspace data and are not the same everywhere.",
		"",
		"Authenticate with `Authorization: Bearer sr_…`. `GET /v1/me` reports who you are "
		"and exactly what you may do, already narrowed by your token — you never need to "
		"work that out by being refused.",
		"",
		"On `PATCH`, a field you omit is left alone and a field you send as `null` is "
		"cleared. That distinction is the only way to clear a due date.",
		"",
		# The `refs` topic below is written for somebody at a terminal, and says useful
		# things an HTTP client does not need — how a shell treats `#`. These are the facts
		# it does not cover, and they belong here rather than there: sharing one text with
		# `subroutine help` is deliberate (§12.2a), and the way to keep that honest is to put
		# audience-specific detail in the audience's own preamble.
		"**An item's `ref` is an integer, and it is how you address one.** "
		"`GET /v1/tasks/42` and `GET /v1/tasks/{id}` are the same request; every "
		"task- and document-addressed endpoint takes either. Refs are unique per workspace "
		"and shared between tasks and documents, they are never reused, and they never "
		"change — not when an item moves between projects. In a request body, a field that "
		"names another item (`target`, `supersedes`, `parent`) takes the same integer, so "
		"you can send back what you were given without converting it.",
		"",
		"**Ask for less.** A full task is 400-600 tokens and most of them are fields you did "
		"not need. `?fields=ref,title,due_at` returns only those; `?format=compact` returns "
		"one aligned line per item, about ten times smaller; `?format=ids` returns the "
		"addresses alone, about two hundred times smaller, which is what you want when you "
		"are deciding what to look at next. The `items`/`page` envelope is the same in all "
		"of them, so pagination does not change. `fields` and `format` cannot be combined. "
		"`GET /v1/meta` lists the selectable fields and formats per entity.",
		"",
		"In prose — a title, a description, a comment — a reference is written `#42`, and "
		"that is what builds the mention index. The sigil belongs to the *text*: do not put "
		"it in a URL, where it would have to be escaped, and do not expect it in the `ref` "
		"field, which is a number.",
		"",
		"**If you read something, think, and then write it, send the version back.** Put "
		"`expected_version` in the body or `If-Match: \"<version>\"` in the header, and a "
		"change made by somebody else in between is refused with a `409` carrying both "
		"version numbers — rather than silently overwriting their work. A human may be "
		"editing the same task in a text editor while you think.",
		"",
	]

	for topic in subroutine.cli.topics.TOPICS:
		sections.append(f"## {topic.name.capitalize()}")
		sections.append("")
		sections.append(topic.summary)
		sections.append("")
		sections.append(topic.body)
		sections.append("")

	return "\n".join(sections)


def _chosen (
	reachable: typing.Sequence[typing.Any], workspace_id: str | None
) -> typing.Any:
	"""Return which workspace's vocabulary to report, or ``None`` when it is ambiguous."""

	if workspace_id is not None:
		wanted = workspace_id.strip()

		for candidate in reachable:
			if str(candidate.id) == wanted or candidate.slug == wanted.lower():
				return candidate

		return None

	return reachable[0] if len(reachable) == 1 else None


def _statuses (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID
) -> dict[str, list[Status]]:
	"""Return every status this workspace has, grouped by what it applies to."""

	model = subroutine.db.models.vocabulary.Status
	grouped: dict[str, list[Status]] = {}

	for row in session.scalars(
		sqlalchemy.select(model)
		.where(model.workspace_id == workspace_id)
		.order_by(model.entity_type, model.position)
	):
		grouped.setdefault(row.entity_type, []).append(
			Status(
				key=row.key, label=row.label, category=row.category, is_default=row.is_default
			)
		)

	return grouped


def _item_types (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID
) -> dict[str, list[Named]]:
	"""Return every item type this workspace has, grouped by what it applies to."""

	model = subroutine.db.models.vocabulary.ItemType
	grouped: dict[str, list[Named]] = {}

	for row in session.scalars(
		sqlalchemy.select(model)
		.where(model.workspace_id == workspace_id)
		.order_by(model.entity_type, model.position)
	):
		grouped.setdefault(row.entity_type, []).append(
			Named(key=row.key, label=row.label, is_default=row.is_default)
		)

	return grouped


def _link_types (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID
) -> list[LinkType]:
	"""Return every link type this workspace has."""

	model = subroutine.db.models.vocabulary.LinkType

	return [
		LinkType(
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
) -> Tags:
	"""Return the most-used tags, capped, and say how many were left out.

	Usage is counted over the tasks this caller can actually see, so a tag used only in a
	private project they are not a member of does not appear — a tag list is a small
	disclosure, but it is one, and there is no reason for it to be the exception.
	"""

	if workspace is None:
		return Tags(items=[], total=0, truncated=False)

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

	return Tags(
		items=[Tag(name=row[0], usage=row[1]) for row in rows[:TAG_LIMIT]],
		total=total or 0,
		truncated=len(rows) > TAG_LIMIT,
	)


def _listings (request: starlette.requests.Request) -> dict[str, Listing]:
	"""Report what each collection endpoint accepts, read from the application itself.

	The filters come out of the generated OpenAPI document and the sort fields out of the
	routers' own ``SORTABLE`` constants, so both are the live values rather than a
	description of them. §9's full filter grammar is specified and not built; publishing it
	here would be publishing a language this installation does not speak.
	"""

	schema = request.app.openapi()
	found: dict[str, Listing] = {}

	for entity, path, sortable, selectable in LISTINGS:
		operation = schema.get("paths", {}).get(path, {}).get("get", {})
		parameters = [
			parameter["name"]
			for parameter in operation.get("parameters", [])
			if parameter.get("in") == "query"
			and parameter["name"] not in NOT_FILTERS
		]

		found[entity] = Listing(
			path=path,
			filters=sorted(parameters),
			sortable=sorted(sortable),
			selectable=sorted(selectable),
			formats=list(subroutine.api.shaping.FORMATS),
		)

	return found


def _grammars () -> dict[str, Grammar]:
	"""Report the small closed languages, read from the parsers that enforce them."""

	return {
		"relative_dates": Grammar(
			description=(
				"A keyword, optionally shifted: <keyword>[+-]<n><unit>. Units are "
				"minutes, hours, days, weeks, months (capital M) and years."
			),
			vocabulary=list(subroutine.domain.dates.KEYWORDS),
			examples=["today", "now+90m", "end_of_week", "start_of_month+1M"],
		),
		"durations": Grammar(
			description=(
				"A number and a unit, largest first, each unit at most once. A unit is "
				"always required."
			),
			vocabulary=[unit for unit, _minutes in subroutine.domain.durations.UNITS],
			examples=["90m", "1h30m", "2d", "1w2d"],
		),
		"capture": Grammar(
			description=(
				"One line, parsed into fields. Anything not recognised stays in the title, "
				"verbatim."
			),
			vocabulary=[
				"#tag",
				"@assignee",
				"!importance (1-5)",
				"~estimate",
				"+project",
				*(f"{word} <date>" for word in sorted(subroutine.domain.capture.PLANNED_WORDS)),
				*(f"{word} <date>" for word in sorted(subroutine.domain.capture.DEADLINE_WORDS)),
			],
			examples=[
				"Renew the domain by friday !4",
				"Fix the header +WEB #bug ~2h @alice",
				"Call the dentist tomorrow",
			],
		),
	}
