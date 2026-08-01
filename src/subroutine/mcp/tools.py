"""What an agent can actually do here, as nine tools.

**Nine, not one per endpoint, and that is the whole design.** A tool's schema is context an
agent carries for its entire session whether it calls the tool or not, so a surface is a
fixed cost paid up front against a variable benefit. ``#14``'s own note records the
measurement that makes this concrete: Beads found 10-50k tokens via MCP against 1-2k via a
CLI. A tool per endpoint would spend the benefit before earning it.

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

import datetime
import typing

import subroutine.clients.base
import subroutine.db.types
import subroutine.directory
import subroutine.domain.capture
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.mcp.protocol
import subroutine.views

#: How many rows a listing returns when the caller does not say. Smaller than the API's
#: fifty: an agent choosing what to do next reads the top of a ranking, and the ones below it
#: are context spent on rows it will not act on.
DEFAULT_LIMIT = 20

#: Named once because it appears on every tool and nine copies of a sentence drift.
#:
#: **It saves no budget, and it would be easy to believe it does.** The dict is shared by
#: reference in this file and serialised in full for each tool, so the wire cost is exactly
#: what nine literals cost — 434 bytes, a tenth of the surface, spent saying the same thing
#: nine times. The only construction that would actually cut it is ``$defs`` plus ``$ref``,
#: and that is not worth betting a client's parser on: one that does not resolve a reference
#: shows a property with no description at all, which is worse than a repeated one.
WORKSPACE = {"type": "string", "description": "Workspace name or id."}


def catalogue (client: subroutine.clients.base.Client) -> list[subroutine.mcp.protocol.Tool]:
	"""Return the tools, bound to one connection."""

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
					"q": {"type": "string", "description": "Find by words, in titles and bodies."},
					"workspace": WORKSPACE,
				},
			},
			call=lambda arguments: _listed(client, arguments),
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
				"conclusion the next session needs, write a document instead."
			),
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": "integer", "description": "The item's number."},
					"body": {"type": "string", "description": "What happened."},
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

	tasks = client.tasks(
		workspace=workspace,
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
		client.documents(workspace=workspace, limit=limit - len(tasks), q=query)
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
	if captured.summary is not None:
		answer = f"{answer}  {captured.summary}"

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
	"""Add one entry to an item's record, whether it is a task or a document."""

	ref = _ref(arguments)
	workspace = _text(arguments, "workspace")
	_, kind = _item(client, ref, workspace)

	client.remark(
		ref=ref,
		body=_text(arguments, "body") or "",
		entity_type=kind,
		workspace=workspace,
	)

	return f"Recorded on #{ref}."


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
		made = client.link(
			ref=ref,
			link_type=link_type,
			target=other,
			entity_type=kind,
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

	for name in ("importance", "urgency", "status", "type", "title"):
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
			"plan or defer."
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
