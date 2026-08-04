"""What an agent can actually do here.

**A handful, not one per endpoint, and that is the whole design.** A tool's schema is context
an agent carries for its entire session whether it calls the tool or not, so a surface is a
fixed cost paid up front against a variable benefit. ``#14``'s own note records the
measurement that makes this concrete: Beads found 10-50k tokens via MCP against 1-2k via a
CLI. A tool per endpoint would spend the benefit before earning it.

**The count lives in ``tests/test_mcp.py`` and deliberately not here.** It said "nine" for as
long as there were more than nine, in the file whose own argument is that the number matters —
which is `#198`'s finding about a figure repeated in four places, met one more time.

So the arguments lean on grammars that already exist. ``add`` takes one captured line
(§6.13) rather than ten typed properties, because the grammar is published, tested, and
already the thing a person types — and one string is a schema an agent reads once and
remembers. ``list`` takes the same ``order=`` spelling §8.4 uses.

**Nothing here opens a database.** Everything goes through ``subroutine.clients``, which is
what the CLI uses, so a session against a remote instance behaves exactly as one against the
local database. Rendering is deliberately terse for the same reason the surface is small:
these strings are read by a model, and a full task is 400-600 tokens of which most are fields
nobody asked for.
"""

import dataclasses
import datetime
import typing

import subroutine.clients.base
import subroutine.db.types
import subroutine.directory
import subroutine.domain.capture
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.installations
import subroutine.mcp.protocol
import subroutine.views

#: How many rows a listing returns when the caller does not say. Smaller than the API's
#: fifty: an agent choosing what to do next reads the top of a ranking, and the ones below it
#: are context spent on rows it will not act on.
DEFAULT_LIMIT = 20

#: Named once because it appears on nearly every tool and copies of a sentence drift.
#:
#: **It saves no budget, and it would be easy to believe it does.** The dict is shared by
#: reference in this file and serialised in full for each tool, so the wire cost is exactly
#: what the same number of literals would cost — roughly a twelfth of the surface, spent saying
#: one thing repeatedly. The only construction that would actually cut it is ``$defs`` plus
#: ``$ref``, and that is not worth betting a client's parser on: one that does not resolve a
#: reference shows a property with no description at all, which is worse than a repeated one.
#:
#: **The figure that used to be here is in `tests/test_mcp.py`** (`#361`). It said "638 bytes
#: across 11 tools as of 2026-08-03" and was 696 across 12 two commits later — stale in the
#: paragraph directly below this module's own note that a count belongs somewhere it can fail.
#: Measuring it in the test is what makes the argument checkable rather than dated.
WORKSPACE = {"type": "string", "description": "Workspace name or id."}

#: Narrowing a listing to one project and everything under it — item `#367`.
#:
#: **An argument rather than a default, and that is the decision rather than the cheap
#: option.** §13.7 settles that context directs *writes* and never narrows what you can see:
#: "forgetting it cannot cost you a missed item". A `.subroutine` marker is context, so it
#: fills in where a task is *filed* (`_added`) and deliberately not what a listing shows —
#: an agent silently blind to work filed next door finds out by not finding something.
#:
#: What was missing is the capability, not the default. `subroutine list --project` has always
#: existed and no tool could ask, so an agent that *wants* to spend its context on one project
#: had no way to say so. That gap is `#149`'s blind spot for the third time: this file's guard
#: compares surfaces per method, and an *argument* on a method both already call is invisible
#: to it.
PROJECT = {"type": "string", "description": "Narrow to this project and everything under it."}


def references (
	client: subroutine.clients.base.Client,
) -> list[subroutine.mcp.protocol.Resource]:
	"""Return the documents an agent may read when it wants them — `#483`.

	**Resources rather than tools, and the difference is the budget.** §21.2 caps the tool
	surface because a schema is context every session carries whether the tool is called or
	not; a resource costs one line in ``resources/list`` and its content only when a model asks.
	The surface was 13/13 tools and 7,916 of 8,800 bytes when this was written, so a
	documentation *tool* was not affordable and this is free.

	The gap it closes: an agent over MCP alone — no shell, no HTTP of its own — had no way to
	read §13.3's guide, which is the document written for precisely that reader. The reach
	guard excused it on the grounds that "somebody holding a client has already got past the
	problem it solves", which was written from the CLI and is untrue here.

	Fetched at read time rather than captured, so this is a route to the instance's copy and
	not a fourth edition of it (`#47`).
	"""

	return [
		subroutine.mcp.protocol.Resource(
			uri="subroutine://docs/agent",
			name="agent-guide",
			title="Working with this instance",
			description=(
				"What this is for, how a ref works, what to read first, and what is not built "
				"yet. Written for an agent rather than adapted from a person's manual."
			),
			mime_type="text/markdown",
			read=lambda: client.reference("agent"),
		),
		subroutine.mcp.protocol.Resource(
			uri="subroutine://docs/examples",
			name="worked-examples",
			title="Worked calls, in the order they are usually needed",
			description=(
				"A real request for each common act, every one executed by this project's own "
				"test suite so an example that stopped working fails the build."
			),
			mime_type="text/markdown",
			read=lambda: client.reference("examples"),
		),
	]


def catalogue (
	client: subroutine.clients.base.Client, *, workspace: str | None = None
) -> list[subroutine.mcp.protocol.Tool]:
	"""Return the tools, bound to one connection and — optionally — to one workspace.

	``workspace`` is what a session is bound to when the plugin names one, and it is a
	*default* rather than a pin: the ``workspace`` argument every tool already carries still
	wins, so an agent that needs to look somewhere else can, and one that says nothing lands
	where the session was pointed (`#333`).

	**Not read from the stored context**, which is the tempting answer and is the one `#276`
	deliberately removed: `subroutine use` is working state that a person moves between tasks,
	and a server reads it once at startup and holds it for the session — so which workspace an
	agent wrote to would depend on where that pointed at the unrelated moment its process
	started. A setting somebody wrote down is a decision they can see.
	"""

	return _within(workspace, _tools(client))


def _within (
	workspace: str | None, tools: list[subroutine.mcp.protocol.Tool]
) -> list[subroutine.mcp.protocol.Tool]:
	"""Give every tool a default workspace, leaving one the caller named alone.

	Done once here rather than in each tool's own reader, so that a tool added later inherits
	it — the failure this closes was a parameter nobody noticed was never supplied, and a fix
	that has to be remembered per tool would be the same shape one layer down.
	"""

	if workspace is None:
		return tools

	def bound (
		tool: subroutine.mcp.protocol.Tool,
	) -> subroutine.mcp.protocol.Tool:
		"""Return one tool with the session's workspace filled in when none was given.

		**Only where the tool declares the argument** (`#379`). This used to fill it in
		everywhere, including `subroutine_whoami`, which declares no properties at all — and
		that was harmless exactly as long as nothing checked. Now that an undeclared argument
		is refused, filling one in would refuse the tool on its first call, in the layer meant
		to be helping.
		"""

		if "workspace" not in tool.schema.get("properties", {}):
			return tool

		call = tool.call

		return dataclasses.replace(
			tool,
			call=lambda arguments: call(
				arguments
				if _text(arguments, "workspace")
				else {**arguments, "workspace": workspace}
			),
		)

	return [bound(tool) for tool in tools]


def _tools (client: subroutine.clients.base.Client) -> list[subroutine.mcp.protocol.Tool]:
	"""Return the tools themselves, bound to one connection."""

	return [
		subroutine.mcp.protocol.Tool(
			name="subroutine_list",
			title="List work",
			description=(
				"List open items — tasks and documents — from the backlog. Default order is "
				"newest first; pass order='-priority_score' for what to work on next, which "
				"ranks assessed items above half-assessed ones above unranked."
			),
			schema={
				"type": "object",
				"properties": {
					"order": {
						"type": "string",
						"description": (
							"Sort fields, comma-separated, '-' to reverse: "
							"'-priority_score', '-due_at', 'title'."
						),
					},
					"limit": {"type": "integer", "description": f"Rows. Default {DEFAULT_LIMIT}."},
					"project": PROJECT,
					"ready": {
						"type": "boolean",
						"description": (
							"Only work that can be started: nothing unfinished blocks it and "
							"it is not deferred. Does not read an item's own status."
						),
					},
					"today": {
						"type": "boolean",
						"description": "The agenda: overdue, due today, and planned next.",
					},
					"workspace": WORKSPACE,
				},
			},
			call=lambda arguments: _listed(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_search",
			title="Search",
			description="Find items by words, in titles and bodies. Tasks and documents both.",
			schema={
				"type": "object",
				"properties": {
					"q": {"type": "string", "description": "Words to look for."},
					"project": PROJECT,
					"limit": {"type": "integer", "description": f"Rows. Default {DEFAULT_LIMIT}."},
					"workspace": WORKSPACE,
				},
				"required": ["q"],
			},
			call=lambda arguments: _searched(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_show",
			title="Read one item",
			description=(
				"Read one task or document in full, with its links and its record. A ref may "
				"name either kind."
			),
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": "integer", "description": "The item's number."},
					"history": {"type": "boolean", "description": "Every change, newest first."},
					"workspace": WORKSPACE,
				},
				"required": ["ref"],
			},
			call=lambda arguments: _shown(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_add",
			title="Add a task",
			description=(
				"Create a task from one line. Dates, tags, priority and estimate are parsed "
				"out of it: 'Fix the boiler by friday !4/2 ~2h #home +SR'. "
				"!importance/urgency are 1-5, ~ is a duration, # is a tag, + is a project. "
				"Anything not recognised stays in the title verbatim."
			),
			schema={
				"type": "object",
				"properties": {
					"text": {"type": "string", "description": "The line to capture."},
					"type": {"type": "string", "description": "task, bug, feature, chore, spike."},
					"description": {
						"type": "string",
						"description": "Why it matters, in full. The title stays one line.",
					},
					"workspace": WORKSPACE,
				},
				"required": ["text"],
			},
			call=lambda arguments: _added(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_comment",
			title="Record what happened",
			description=(
				"Add to an item's record of what happened — what you did, what you found, "
				"what failed. A '#42' in the body becomes a link on item 42. For a "
				"conclusion the next session needs, write a document instead. Pass "
				"remove=true with words from a comment to take it back out."
			),
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": "integer", "description": "The item's number."},
					"body": {"type": "string", "description": "What happened."},
					"remove": {"type": "boolean", "description": "Withdraw it instead."},
					"workspace": WORKSPACE,
				},
				"required": ["ref", "body"],
			},
			call=lambda arguments: _remarked(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_document",
			title="Write a document",
			description=(
				"Record a conclusion the next session needs — a decision, a finding, a "
				"design, a dead end. A comment is what happened; a document is what you "
				"concluded. A '#42' in the body becomes a link on item 42."
			),
			schema={
				"type": "object",
				"properties": {
					"title": {"type": "string", "description": "What it concludes, in one line."},
					"body": {"type": "string", "description": "The reasoning, in Markdown."},
					"type": {
						"type": "string",
						"description": "note, spec, design, decision, finding or dead_end.",
					},
					"project": {"type": "string", "description": "Project key."},
					"workspace": WORKSPACE,
				},
				"required": ["title"],
			},
			call=lambda arguments: _wrote(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_update",
			title="Change a task",
			description=(
				"Change a task: priority, estimate, status, title, or the day it is planned "
				"for or hidden until. Set both priority axes — one alone sorts below "
				"everything ranked. Omitted fields are unchanged."
			),
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": "integer", "description": "The task's number."},
					"importance": {"type": "integer", "description": "1-5, 5 highest."},
					"urgency": {"type": "integer", "description": "1-5, 5 soonest."},
					"estimate": {"type": "string", "description": "How long, e.g. '4h'."},
					"status": {"type": "string", "description": "A status key, e.g. in_progress."},
					"type": {"type": "string", "description": "task, bug, feature, chore, spike."},
					"title": {"type": "string", "description": "A new title."},
					"description": {
						"type": "string",
						"description": (
							"What it is about, in full. This is where the reasoning behind an "
							"outcome-shaped title goes."
						),
					},
					"plan": {"type": "string", "description": "The day to do it. A date or ''."},
					"defer": {
						"type": "string",
						"description": "Hide it until this day. '' to unhide.",
					},
					"workspace": WORKSPACE,
				},
				"required": ["ref"],
			},
			call=lambda arguments: _updated(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_link",
			title="Join two items",
			description=(
				"Say how two items are related: blocks, relates_to, duplicates, derives_from. "
				"'blocks' is what readiness reads — a task with an unfinished blocker is not "
				"listed as ready. Pass remove=true to withdraw the link instead."
			),
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": "integer", "description": "The item's number."},
					"type": {"type": "string", "description": "blocks, relates_to, duplicates."},
					"other": {"type": "integer", "description": "The other item's number."},
					"remove": {"type": "boolean", "description": "Withdraw it instead."},
					"workspace": WORKSPACE,
				},
				"required": ["ref", "other"],
			},
			call=lambda arguments: _linked(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_project",
			title="Projects",
			description=(
				"List the projects, or make one by passing a key and a title. A key is "
				"permanent and starts with a letter, like WEB. Work is filed under a project "
				"with '+KEY' in a captured line."
			),
			schema={
				"type": "object",
				"properties": {
					"key": {"type": "string", "description": "Its permanent short name."},
					"title": {"type": "string", "description": "What it is called."},
					"parent": {"type": "string", "description": "Put it inside this project."},
					"private": {"type": "boolean", "description": "Only members can see it."},
					"workspace": WORKSPACE,
				},
			},
			call=lambda arguments: _projected(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_changes",
			title="What changed",
			description=(
				"What has changed since you last looked, oldest first. Ask at the start of a "
				"session: nothing here tells you when your own knowledge went stale. Pass the "
				"seq of the last event you saw back as 'since' — it is inclusive, so you will "
				"see that one again."
			),
			schema={
				"type": "object",
				"properties": {
					"since": {
						"type": "integer",
						"description": "Resume from this seq, inclusive. Omit for the newest.",
					},
					"mine": {
						"type": "boolean",
						"description": "Only what this credential itself did.",
					},
					"limit": {"type": "integer", "description": f"Rows. Default {DEFAULT_LIMIT}."},
					"workspace": WORKSPACE,
				},
			},
			call=lambda arguments: _changed(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_claim",
			title="Take a task",
			description=(
				"Take a task so another worker does not start it too, or give one back. "
				"'ready' listings hide what somebody else holds and never hide your own. A "
				"claim expires by itself, so say it again while you are still working."
			),
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": "integer", "description": "The task's number."},
					"release": {
						"type": "boolean",
						"description": "Give it back instead of taking it.",
					},
					"workspace": WORKSPACE,
				},
				"required": ["ref"],
			},
			call=lambda arguments: _claimed(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_whoami",
			title="Who am I",
			description=(
				"The account these tools act as, and what it may do. Worth asking before your "
				"first write: a shell you run resolves its own credential and can be a "
				"different principal, which is silent when it is wrong."
			),
			schema={"type": "object", "properties": {}},
			call=lambda arguments: _whoami(client),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_done",
			title="Finish a task",
			description="Mark a task complete by its ref number.",
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": "integer", "description": "The task's number."},
					"workspace": WORKSPACE,
				},
				"required": ["ref"],
			},
			call=lambda arguments: _completed(client, arguments),
		),
	]


def _claimed (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Take a task or give it back, and say which — item ``#350``.

	**One tool with a ``release`` flag rather than two, and that is a budget decision made
	against `#149`'s lesson rather than in ignorance of it.** A capability parked in another
	tool's argument is undiscoverable — a model reads tool *names* to decide what it can do,
	which is why searching became its own tool. What makes this different is that taking and
	giving back are the same capability in two directions, named together in one description a
	model reads whole: an agent that has found "take a task" has found how to give it back, in
	a way it could never have found ``list(q=…)`` from a tool called ``list``.

	The alternative was measured at roughly two hundred more bytes on every session, for a
	verb agents call rarely and only after calling this one.
	"""

	ref = _ref(arguments)
	workspace = _text(arguments, "workspace")

	if arguments.get("release"):
		freed = client.release(ref=ref, workspace=workspace)

		return f"Released #{freed.ref}  {freed.title}"

	held = client.claim(ref=ref, workspace=workspace)
	until = (
		""
		if held.claim_expires_at is None
		else f", until {held.claim_expires_at.isoformat(timespec='minutes')}"
	)

	return f"Claimed #{held.ref}  {held.title}{until}"


def _whoami (client: subroutine.clients.base.Client) -> str:
	"""Return which principal these tools act as, and what it may do — item ``#347``.

	**A tool rather than a line in the server instructions**, and the reason is that the two
	answer different questions. The instructions name the *connection*, which is an endpoint;
	this names the *principal*, which is not implied by it. That was measured rather than
	assumed (`#346`): an agent on one machine, in one session, on one connection, wrote as a
	bounded service account through these tools and as a superuser through its shell.

	It is also why this exists at all. Before it, the only way for an agent to learn its own
	identity here was to write to a real item and read the author back — an identity check
	whose method is a side effect on production data is one nobody runs before their first
	write, which is when it matters.

	Terse like everything else in this module: the fields are the ones that change what an
	agent should do, and the permissions are printed only where the credential narrowed them,
	because an unnarrowed owner would otherwise be handed twenty keys it already holds.
	"""

	me = client.me()
	credential = me.credential
	kind = "agent" if me.user.is_service_account else "person"
	how = (
		"the local database"
		if credential is None
		# The ellipsis is not decoration: a prefix is the *public half* of a credential (§7.4),
		# and printing it bare invites a reader to think they are looking at a short token. The
		# CLI's `whoami` has always said it this way and this had not (`#361`).
		else f"token {credential.title!r} ({credential.prefix}…)"
	)
	lines = [f"{me.user.username} ({kind}), via {how}."]

	if credential is not None and credential.narrows:
		lines.append(
			f"Narrowed to {subroutine.views.narrowing(credential, me.workspaces)}."
		)

	if me.instance_permissions:
		lines.append(f"Over the installation: {', '.join(me.instance_permissions)}.")

	if not me.workspaces:
		# The failure worth naming rather than rendering as an empty list: every other tool
		# would report this credential's reach as an instance with nothing in it.
		lines.append("No workspace here can be read with this credential.")

	else:
		lines.append("")
		lines.extend(
			f"{workspace.slug}  {workspace.role or 'no role'}"
			+ (
				f"  may: {', '.join(workspace.permissions)}"
				if workspace.narrowed_by_credential
				else ""
			)
			for workspace in me.workspaces
		)

	# **The early return this used to take is gone, deliberately** (`#381`). A credential that
	# reaches no workspace is the single most likely reason somebody asks this question, and
	# it was the one branch that would have answered without saying which versions were in
	# play — the answer missing from exactly the case that needs it.
	lines.append("")
	lines.extend(
		subroutine.views.versions(
			me,
			program=subroutine.installations.program(),
			plugin=subroutine.installations.plugin(),
		)
	)

	return "\n".join(lines)


def _changed (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Return the feed as one compact line per event, ending with the number to resume from.

	**The resume number is printed even when nothing changed**, because an agent that polled
	and saw nothing still needs somewhere to carry on from — and if it has to infer that from
	an empty list it will infer wrongly on the one occasion it matters.
	"""

	since = arguments.get("since")
	# Same reading of `limit` as `_listed`: an explicit zero is passed through to
	# `domain.paging.size`, which is the one arbiter of a page size and refuses it by name.
	given = arguments.get("limit")

	events = client.changes(
		since=since,
		mine=bool(arguments.get("mine")),
		newest=since is None,
		workspace=_text(arguments, "workspace"),
		limit=DEFAULT_LIMIT if given is None else given,
	)

	if not events:
		return "Nothing has changed."

	lines = [
		f"{event.seq}  {event.created_at.astimezone():%d %b %H:%M}  {_happened(event)}  "
		f"{_named(event)}"
		for event in events
	]

	return "\n".join([*lines, f"Resume with since={events[-1].seq}."])


def _named (event: subroutine.views.Event) -> str:
	"""Return the item an event is about, as short as it can be said.

	``item_ref``/``item_title`` are on the view so that this, the CLI and any browser name a
	row the same way rather than each resolving the id again.
	"""

	if event.item_ref is None:
		return event.item_title or event.entity_type

	return f"{subroutine.domain.refs.format_ref(event.item_ref)} {event.item_title}"


def _searched (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Find items by words, sharing the renderer the listing uses.

	**A verb of its own rather than an argument on the listing** (`#282`, Simon's decision).
	The CLI has had ``subroutine search`` since it had ``list``, and this surface carried the
	same capability as ``list(q=…)`` — so an agent that learned one surface was taught
	something false about the other, and three attempts at the CLI failed with messages
	naming neither. Two surfaces disagreeing about what a verb is called is the same family
	as `#276` and `#278`: a true statement that misleads about the system.

	The schema requires ``q``, which stops the empty call; this refuses a *blank* one, which
	the schema cannot see and which would otherwise answer "find nothing" with the whole
	backlog.

	``_text`` is deliberately not consulted for that check: it treats ``"   "`` as given,
	because for every other argument a string of spaces is a value somebody meant. Here it is
	the empty call wearing a disguise, and letting it through searched for whitespace and
	reported "Nothing open." — an answer about the backlog to a question about a word.
	"""

	if not (arguments.get("q") or "").strip():
		return "Say what to look for."

	return _listed(client, arguments)


def _listed (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Return the backlog as one compact line per item.

	Tasks *and* documents, because one counter per workspace serves both (§6.2) — a list
	holding only tasks tells a reader who has learned that a number names an item that half
	the numbers do not exist.
	"""

	workspace = _text(arguments, "workspace")

	# `or DEFAULT_LIMIT` read an explicit `limit: 0` as "unset" and returned twenty. Zero is
	# a strange thing to ask for and a stranger thing to answer with twenty, so it is passed
	# through to `domain.paging.size`, which is the one arbiter of what a page may be and
	# refuses it by name.
	given = arguments.get("limit")
	limit = DEFAULT_LIMIT if given is None else given

	# **The one question this answers that a list of tasks does not** (`#136`, §6.5a). It
	# reached the HTTP endpoint and nothing else until then, so an agent asked what to work on
	# could only ever sort a backlog — which is what every other tool offers.
	ready = bool(arguments.get("ready"))
	query = _text(arguments, "q")

	# **The agenda is a different question, so it is a different call and the same renderer.**
	# `today` asks what is on now — overdue, due, planned, in progress — which no ordering of
	# a backlog produces, because "overdue" is a comparison against the clock rather than a
	# sort key. Returned flat: the buckets are a *terminal* structure, and a model reading
	# four headings for what is usually four rows is paying for the headings.
	if arguments.get("today"):
		agenda = client.agenda(workspace=workspace)

		# **Three buckets, not four: `unscheduled` is deliberately left out.** It is the
		# terminal's filler — "your day is empty, here is some backlog" — capped at twenty,
		# and none of it is *on today*. Concatenating it answered "what is on today" with the
		# whole backlog, which was measured against the real instance rather than reasoned
		# about. An agent whose day is empty should ask `ready=true`, which is the better
		# question and already a cheaper one.
		rows = [
			_line(task)
			for bucket in (agenda.overdue, agenda.today, agenda.upcoming)
			for task in bucket
		]

		return "\n".join(rows) if rows else "Nothing on today."

	project = _text(arguments, "project")
	tasks = client.tasks(
		workspace=workspace,
		project=project,
		limit=limit,
		order=_text(arguments, "order"),
		ready=ready,
		q=query,
	)

	# **`limit` bounds the answer, not each kind.** Asking for five and receiving five tasks
	# followed by five documents is the caller's budget spent twice, which for an agent is
	# the whole cost of the call. Tasks first because a ranking is what the limit is usually
	# for, and documents fill whatever is left.
	# **Never documents when `ready` was asked for.** §6.14 says a document is not scheduled
	# and nothing blocks one, so every specification and decision in the instance would report
	# as ready — true, useless, and enough of them to bury the tasks the caller asked about.
	documents = (
		client.documents(
			workspace=workspace, project=project, limit=limit - len(tasks), q=query
		)
		if len(tasks) < limit and not ready
		else []
	)
	rows = [_line(task) for task in tasks] + [_line(document) for document in documents]

	if not rows:
		return "Nothing open."

	return "\n".join(rows)


def _line (item: subroutine.views.Task | subroutine.views.Document) -> str:
	"""Return one item as a line: address, kind, rank, estimate, title.

	Assembled here rather than reusing ``?format=compact``, which is a *terminal* rendering —
	aligned columns with long titles cut short. A model reading a truncated title has been
	given damaged data to save characters it did not need saving.
	"""

	cells = [subroutine.domain.refs.format_ref(item.ref), item.type]

	if isinstance(item, subroutine.views.Task):
		# **Before the rank, because it changes what the rank means** (`#425`). A default
		# listing put a blocked item above the thing blocking it with nothing to say so, and an
		# agent reading one reported it as "start with #2". `ready=true` filters correctly; the
		# listing an agent gets by asking for the backlog is the one that could not tell it.
		if item.blocked:
			cells.append("blocked")

		if item.importance is not None or item.urgency is not None:
			cells.append(f"!{item.importance or '?'}/{item.urgency or '?'}")

		if item.estimate_human is not None:
			cells.append(item.estimate_human)

		if item.due_at is not None:
			cells.append(f"due {item.due_at.date().isoformat()}")

	cells.append(item.title)

	return "  ".join(cells)


def _happened (event: subroutine.views.Event) -> str:
	"""Return one event as a phrase, in the same words the CLI uses.

	Behind an argument rather than always: a history is unbounded where a comment list is
	bounded by what somebody typed, and most items have one event saying they were created.
	Spending that on every ``show`` is the cost §14 exists to weigh.
	"""

	if event.subject_type is not None:
		return {"created": "commented", "updated": "edited a comment"}.get(
			event.action, f"{event.action} a comment"
		)

	if event.action != "updated" or not event.changes:
		return event.action

	return "changed " + ", ".join(sorted(event.changes))


def _item (
	client: subroutine.clients.base.Client, ref: int, workspace: str | None
) -> tuple[subroutine.views.Task | subroutine.views.Document, str]:
	"""Return the item a ref names, and which kind it turned out to be.

	One counter per workspace serves tasks and documents (§6.2), so a ref alone does not say
	which it is and every tool taking one has to ask. Shared rather than repeated because the
	two callers had drifted: ``show`` resolved both kinds and ``comment`` assumed a task, so
	an agent could read a document's record and not add to it — while the tool beside them
	told it to write documents in the first place (`#145`).
	"""

	found: subroutine.views.Task | subroutine.views.Document | None = client.task(
		ref=ref, workspace=workspace
	)

	if found is not None:
		return found, "task"

	document = client.document(ref=ref, workspace=workspace)

	if document is None:
		raise LookupError(f"There is no #{ref} here.")

	return document, "document"


def _shown (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Return one item in full, with its links and its record."""

	ref = _ref(arguments)
	workspace = _text(arguments, "workspace")
	found, kind = _item(client, ref, workspace)

	parts = [_line(found)]
	body = (
		found.description if isinstance(found, subroutine.views.Task) else found.body
	)

	if body:
		parts.append("")
		parts.append(body)

	links = client.links(ref=ref, entity_type=kind, workspace=workspace)

	if links:
		parts.append("")
		parts.extend(
			f"{link.label}  #{link.other.ref}  {link.other.title}" for link in links
		)

	if arguments.get("history"):
		parts.append("")
		parts.extend(
			f"{event.created_at.date().isoformat()}  {_happened(event)}"
			for event in client.history(ref=ref, entity_type=kind, workspace=workspace)
		)

	remarks = client.comments(ref=ref, entity_type=kind, workspace=workspace)

	if remarks:
		parts.append("")
		# **The date, not the author's UUID.** A raw id is thirty-six characters a model
		# cannot resolve without another call, on every comment, in the module whose whole
		# argument is that context is a fixed cost. When an item's record is read, *when*
		# something happened is the part that orders it; *who* is one id lookup away and is
		# usually the reader.
		parts.extend(
			f"{remark.created_at.date().isoformat()}  {remark.body}" for remark in remarks
		)

	return "\n".join(parts)


def _added (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Capture one line as a task, and say what was understood — **and what was not**.

	Reports what the grammar took, because a caller that cannot see the parse cannot tell a
	deadline that was read from one that stayed in the title.

	**And what it declined to read**, which §6.13 rule 1 requires and this dropped until
	`#115`. An agent writing "Water the plants every monday" was told "Added #1 task Water the
	plants every monday" and had no way to learn that the recurrence was not set up — so the
	one caller most likely to believe it had been was the one not told. The CLI had said so on
	both its paths for exactly that reason.

	Not `isError`: the task was created and the answer is a success. The line is added only
	when there is something to say, so an ordinary capture costs nothing.
	"""

	# **Looked for on every call rather than at startup** (§13.7a, `#159`). A stdio server
	# outlives the moment it was launched, and a repository adopted mid-session should not
	# need it restarted — which is the one thing an agent cannot do to itself.
	marker = subroutine.directory.find()
	line = _text(arguments, "text") or ""
	workspace = _text(arguments, "workspace")

	consulted = (
		marker is not None
		# **And only where the marker speaks for the connection this session is on** (`#414`).
		# A marker names one instance; its project is a fact about that instance and nothing
		# else. Without this, `directory.resolve`'s match-by-key fallback — which exists for
		# markers written before `#177` gave them ids — filed work into a same-named project on
		# whichever instance happened to answer.
		and marker.speaks_for(client.connection.name)
		and (marker.project is not None or marker.project_id is not None)
		and not subroutine.domain.capture.names_a_project(line)
	)

	# **Resolved against this instance, never passed through** (`#232`). The marker's key went
	# straight to the server until 0.1.0, so a checkout marked for somebody else's instance —
	# which is what committing this file is *for* — refused every `subroutine_add` with "there
	# is no project 'SR' here", while the CLI beside it filed the task and said it had ignored
	# the marker. `#166` settled that the marker is advisory; only one surface implemented it.
	# Resolving also buys `#177`: a renamed project is followed by id, which this never did.
	filed = (
		subroutine.directory.resolve(marker, client.projects(workspace=workspace))
		if consulted and marker is not None
		else None
	)

	captured = client.capture(
		text=line,
		workspace=workspace,
		type=_text(arguments, "type"),
		project=filed,
		# **The second call an agent was measured skipping** (`#424`). `#392` put this on
		# `subroutine_update`, which made a described item two calls on two tools — and the
		# agent that reported this one said plainly why that loses: "an agent weighing calls
		# will systematically skip an optional second write, and the moment you have the most
		# context about an item is when you file it".
		description=_text(arguments, "description"),
	)
	answer = "Added " + _line(captured.task)

	# Said out loud for the same reason the CLI says it: nobody typed it, and an agent that
	# cannot see where its work went cannot tell a person either. That argument applies just
	# as much when the marker was *not* used — more so, because the agent is then holding a
	# repository whose file says one thing and an instance that says another.
	if filed is not None:
		answer = f"{answer}\n  in {filed}, from {subroutine.directory.FILE_NAME}"

	elif consulted and marker is not None:
		shown = marker.project or marker.project_id

		answer = (
			f"{answer}\n  {subroutine.directory.FILE_NAME} here names {shown!r}, which is not "
			f"on this instance. Ignoring it."
		)

	# Both halves of §6.13's obligation, and `#135` is why the second one is here: an agent is
	# the caller most likely to have written something it believes was understood, and telling
	# it only what was *left* answers the rarer question.
	# **And parenthesised, because this line already carries a rank** (`#426`). `!4/3` appeared
	# twice with two meanings — the item's priority and the token that set it — with nothing but
	# a double space between the title and either of them.
	echoed = subroutine.domain.capture.read_back(captured.summary)

	if echoed is not None:
		answer = f"{answer}  {echoed}"

	left = subroutine.domain.capture.explain(captured.unparsed)

	return answer if left is None else f"{answer}\n{left}"


def _wrote (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Write a document and name it back by the ref it was given.

	**The tool this adapter told agents to use and did not have** (`#138`). Until 2026-07-31
	``subroutine_comment``'s own description said "for a conclusion the next session needs,
	write a document instead", and there was no way to — a sentence in the agent-facing surface
	pointing at something that surface could not do.
	"""

	document = client.create_document(
		title=_text(arguments, "title") or "",
		body=_text(arguments, "body"),
		type=_text(arguments, "type"),
		project=_text(arguments, "project"),
		workspace=_text(arguments, "workspace"),
	)

	return "Wrote " + _line(document)


def _remarked (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Add one entry to an item's record, or withdraw one — whether task or document.

	**One tool for both directions**, which is the shape ``subroutine_link`` already
	established (`#141`): withdrawing ships with making, and a second tool would spend a name
	and a schema in every session's context on the inverse of a verb the caller has already
	found. §21.2's budget is at its cap, and a boolean is about ninety bytes where a tool is
	four hundred.

	**Named by its words, never by an id** (`#400`), for the same reason the CLI is: a comment
	has no number of its own, and its id appears in nothing a caller has necessarily read.
	Matching more than one is refused rather than guessed at, because the alternative is
	deleting somebody's prose on a coin toss.
	"""

	ref = _ref(arguments)
	workspace = _text(arguments, "workspace")
	body = _text(arguments, "body") or ""
	_, kind = _item(client, ref, workspace)

	if not arguments.get("remove"):
		client.remark(ref=ref, body=body, entity_type=kind, workspace=workspace)

		return f"Recorded on #{ref}."

	# **The words are the address, so an empty one is refused rather than matched** (`#415`).
	# `"" in anything` is true, so this used to name every comment on the item — and with one
	# comment there, "more than one says that" had nothing to catch. `remove=true` and no
	# `body` at all answered "Taken out of #1."
	recorded = subroutine.views.comments_saying(
		client.comments(ref=ref, entity_type=kind, workspace=workspace), body
	)

	if not recorded:
		raise ValueError(f"Nothing recorded on #{ref} says that.")

	if len(recorded) > 1:
		raise ValueError(
			f"{len(recorded)} comments on #{ref} say that. Pass more of the one you mean."
		)

	client.uncomment(
		ref=ref, comment_id=str(recorded[0].id), entity_type=kind, workspace=workspace
	)

	return f"Taken out of #{ref}."


def _completed (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Finish a task."""

	finished = client.complete(ref=_ref(arguments), workspace=_text(arguments, "workspace"))

	return f"Done: {finished.title}"


def _ref (arguments: dict[str, typing.Any]) -> int:
	"""Return the ref an argument names, accepting ``42`` and ``"#42"`` alike.

	A model reads ``#42`` everywhere this system writes an address, so it will send that back
	sooner or later — and refusing it over a sigil would be refusing the caller its own
	notation (§6.2).
	"""

	given = arguments.get("ref")

	if isinstance(given, bool) or given is None:
		raise ValueError("Which item? Pass 'ref', the number in the listing.")

	found = subroutine.domain.refs.parse_ref(str(given))

	if found is None:
		# `parse_ref` returns None for anything that cannot *be* a ref — a zero, a leading
		# zero, a number too large for the column. Refused here with the value in it, rather
		# than passed on as a lookup that would come back "there is no such item" about
		# something that was never an item.
		raise ValueError(f"{given!r} is not an item number.")

	return found


def _day (given: typing.Any, *, field: str) -> datetime.date | None:
	"""Read a day an agent named, or ``None`` to clear it.

	**The same grammar a person types** (§6.13, `domain.schedule.interpret_written_day`) —
	'today', 'friday', '2026-09-01' — rather than a stricter machine format. An agent working
	from a conversation has "next tuesday" in front of it, and a surface that made it convert
	would be asking it to reimplement a parser this product publishes.

	That sentence was here, in these words, while ``interpret_day`` refused every weekday in
	it (`#167`). It is the same defect the CLI had and it needed the same fix, because the
	argument above is about the *reader*, not about the transport: an agent quoting a
	conversation and a person at a prompt are typing the same words.

	An empty string clears, which is how §8.3's null reaches a schema whose property is a
	string. Omitting the argument is what leaves the day alone.
	"""

	if not isinstance(given, str):
		raise ValueError(f"{field!r} is a day, written like 'friday' or '2026-09-01'.")

	if not given.strip():
		return None

	# **The refusal is the domain's, not one written here** — `interpret_written_day` names
	# the whole typed vocabulary, weekdays first, so an agent and a person are told the same
	# thing in the same words. A second message here would be a place for the two to drift.
	return subroutine.domain.schedule.interpret_written_day(
		given,
		# **The client's own zone, which for a stdio adapter is the machine the agent runs
		# on.** An agent saying "friday" means the Friday it is looking at, and resolving that
		# in UTC turns it into Thursday for anybody west of Greenwich after four in the
		# afternoon — the same westward-drift bug `defer` already met once.
		timezone=str(subroutine.db.types.utcnow().astimezone().tzinfo),
		now=subroutine.db.types.utcnow(),
		field=field,
	)


def _linked (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Join two items, or withdraw the join.

	**Named by the two refs, never by the link's id**, exactly as the CLI is: an id is a UUID
	that appears in nothing a caller has read, so requiring one would make withdrawing a link
	reachable only by something that had just created it.

	One tool for both directions rather than two, because `#141` established that withdrawing
	ships with making — an unwanted link narrows what looks startable and says nothing about
	having done so — and a second tool would spend a name and a schema on the same pair of
	numbers.
	"""

	ref = _ref(arguments)
	workspace = _text(arguments, "workspace")
	other = arguments.get("other")

	if not isinstance(other, int) or isinstance(other, bool):
		raise ValueError("Which other item? Pass 'other', the number in the listing.")

	_, kind = _item(client, ref, workspace)

	if not arguments.get("remove"):
		link_type = _text(arguments, "type") or "blocks"

		# **Both ends are looked up, not just the near one** (`#491`). A ref names a task *or* a
		# document (§6.2), and `client.link` defaults `target_type` to "task" — so naming a
		# document here reported that there was no such task, about an item the caller had just
		# listed. The CLI passes `target_type=far.entity_type` and this did not: one rule carried
		# to one side of a pair, which `#412` found three times in one review.
		_, other_kind = _item(client, other, workspace)

		made = client.link(
			ref=ref,
			link_type=link_type,
			target=other,
			entity_type=kind,
			target_type=other_kind,
			workspace=workspace,
		)

		return f"{made.label} #{made.other.ref}  {made.other.title}"

	joins = [
		one
		for one in client.links(ref=ref, entity_type=kind, workspace=workspace)
		if one.other.ref == other
	]

	if not joins:
		raise LookupError(f"#{ref} is not joined to #{other}.")

	for join in joins:
		client.unlink(ref=ref, link_id=str(join.id), entity_type=kind, workspace=workspace)

	return f"Withdrew the link between #{ref} and #{other}."


def _projected (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""List the projects, or make one.

	**One tool doing both**, which is a budget decision rather than an elegance: SPEC §21.5's
	adoption asks what exists and then adds to it, so the two questions arrive together and a
	second name would be schema spent on the seam between them. Creating is what a key and a
	title mean; asking for neither is asking what is there.
	"""

	workspace = _text(arguments, "workspace")
	key = _text(arguments, "key")

	if key is None:
		rows = client.projects(workspace=workspace)

		if not rows:
			return "No projects."

		return "\n".join(f"{row.key}  {row.title}" for row in rows)

	title = _text(arguments, "title")

	if title is None:
		raise ValueError("A project needs a title as well as a key.")

	made = client.create_project(
		key=key,
		title=title,
		parent=_text(arguments, "parent"),
		visibility="private" if arguments.get("private") else "public",
		workspace=workspace,
	)

	return f"Made {made.key}  {made.title}"


def _text (arguments: dict[str, typing.Any], name: str) -> str | None:
	"""Return one string argument, or ``None`` when it was not given."""

	value = arguments.get(name)

	return value if isinstance(value, str) and value else None


def _updated (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Change a task's own fields, and report what it looks like now.

	**Only the fields an agent actually re-decides**, not everything ``PATCH /v1/tasks``
	accepts. Every property here is schema carried by every session of every agent, including
	the ones that never call this, so the dates and the assignee stay off it — those are
	``schedule``'s and a person's respectively.

	Nothing given is a refusal rather than a no-op: an agent that meant to change something
	and named no field has made a mistake, and a cheerful "unchanged" would hide it.
	"""

	changes: dict[str, typing.Any] = {}

	# **`description` is here because the skill's own argument depends on it** (`#392`). It
	# tells an agent to write an outcome-shaped title on the grounds that "your motivation is
	# not lost, because it belongs in the description — which is one field away". From this
	# surface it was not one field away, it was unreachable, so the skill asked an agent to
	# give up its reasoning and pointed at a shelf it could not put anything on. Reported by
	# an agent that met it and put the context in comments instead — the wrong shelf, and it
	# said so (§5.10).
	for name in ("importance", "urgency", "status", "type", "title", "description"):
		if name in arguments:
			changes[name] = arguments[name]

	if "estimate" in arguments:
		changes["estimate"] = arguments["estimate"]

	days = {
		field: _day(arguments[field], field=field)
		for field in ("plan", "defer")
		if field in arguments
	}

	if not changes and not days:
		raise ValueError(
			"Nothing to change. Pass importance, urgency, estimate, status, type, title, "
			"description, plan or defer."
		)

	ref = _ref(arguments)
	workspace = _text(arguments, "workspace")

	# **Two calls, because they are two endpoints** — `PATCH /v1/tasks` and the scheduling
	# verbs §12.2's `plan` and `defer` reach. Folding them into one client method here would
	# be this surface inventing a shape the others do not have, which is the divergence
	# `#146` measured rather than a fix for it.
	changed = (
		client.update(ref=ref, workspace=workspace, **changes)
		if changes
		else client.task(ref=ref, workspace=workspace)
	)

	if days:
		changed = client.schedule(
			ref=ref,
			workspace=workspace,
			**{
				name: days[field]
				for field, name in (("plan", "planned_for"), ("defer", "start"))
				if field in days
			},
		)

	if changed is None:
		raise LookupError(f"There is no #{ref} here.")

	return "Changed " + _line(changed)
