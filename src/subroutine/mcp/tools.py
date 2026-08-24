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
import json
import posixpath
import typing
import urllib.parse

import subroutine.addressing
import subroutine.clients.base
import subroutine.config
import subroutine.db.types
import subroutine.directory
import subroutine.domain.agenda
import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.documents
import subroutine.domain.filtering
import subroutine.domain.ordering
import subroutine.domain.readiness
import subroutine.domain.recurrence
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.text
import subroutine.errors
import subroutine.installations
import subroutine.mcp.protocol
import subroutine.permissions
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

#: Named separately in the description because it is not a date and the derived list would
#: otherwise call it one — which the first version did, having been written when every
#: filterable field was a timestamp. A description built from a registry still has to say what
#: the registry means.
_DATED = frozenset({subroutine.domain.filtering.TOUCHED_AT})


def _fields_of (
	kind: subroutine.domain.filtering.Kind,
	without: frozenset[str] = frozenset(),
) -> str:
	"""List a task's filterable fields of one kind, for the schema below.

	**Not ``_named``**, which this was called first and which is already the name of the
	function rendering an event's item further down — so the schema was built from one and the
	change feed from the other, by luck of definition order. Caught by mypy rather than by any
	test, because both happened to work.
	"""

	return ", ".join(
		sorted(
			name
			for name, field in subroutine.domain.filtering.TASK_FILTERS.items()
			if field.kind is kind and name not in without
		)
	)


#: Asking a listing about a date — `#815`, Simon's decision of 2026-08-11 to spend the budget.
#:
#: **Built from `domain/filtering`'s registry rather than written out**, so it cannot advertise
#: a field the instance refuses or omit one it accepts. That is what `#815` itself cost twice:
#: `/v1/meta` nearly published `created_at.eq`, and the agent guide nearly hard-coded an
#: operator list that had moved the day before.
DATE_FILTER = {
	"type": "object",
	"additionalProperties": {"type": "string"},
	"description": (
		"Narrow by when, by whom and by how long: {'created_at.gte': 'yesterday'}; two "
		"entries make a range. gt/gte/lt/lte on "
		f"{_fields_of(subroutine.domain.filtering.INSTANT, _DATED)}. "
		# **`#319`, and it is named here because `#821` is what happens otherwise**: a field
		# accepted and unpublished is one an agent never learns, because it does not send the
		# word and get corrected — it never sends it. 48 bytes, from slack rather than by
		# moving the cap, and it is the one filter that answers *what can I finish now*.
		f"{_fields_of(subroutine.domain.filtering.DURATION)} takes '2h' or '90'. "
		"touched_at is *worked on* — a comment or status change counts, which no other "
		"field sees. touched_by takes a username and pairs with it."
	),
}

#: The type of an argument that names an item — `#549`. **Both spellings, because both work
#: and only one was published.**
#:
#: §6.2 requires ``#42`` to be accepted: this system prints that form in every listing it
#: returns, so a model sends it back, and refusing our own notation is a refusal the caller
#: cannot learn from. The schema said ``integer`` alone, which made the accepted form invisible
#: to a client reading the contract and impossible for a strict one to send — harmless while
#: nothing checked, and the first thing to break when something did.
#:
#: Declared once and used seven times, for the reason ``WORKSPACE`` is: seven copies of a type
#: are seven chances for one of them to disagree. It costs 84 bytes of the tool budget, spent
#: from slack rather than by moving the cap; ``tests/test_mcp.py`` holds the measurement.
A_REF = ["integer", "string"]

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

#: A tool that only reads — item `#489`.
#:
#: **The default is the pessimistic one**, so silence here is not neutrality. A client reading an
#: unannotated tool is told by the specification to assume it may destroy things, and clients
#: increasingly turn that into an approval prompt. The five tools carrying this are the ones an
#: agent calls *first*, before it knows what this instance is — so the cost of the wrong default
#: falls on first contact, which is the moment §1.4 cares most about.
READS = {"readOnlyHint": True}

#: A tool that writes, and only ever adds.
#:
#: ``destructiveHint`` asks whether a call *overwrites or deletes* rather than creating or
#: appending. Filing a task, writing a document, adding a comment and completing something are
#: all additive — nothing here removes what somebody else wrote.
#:
#: **Deliberately not claimed for `subroutine_update` or `subroutine_link`**, which replace field
#: values and withdraw links respectively. Those keep the pessimistic default, which is correct
#: for them rather than merely unstated.
#:
#: **`readOnlyHint: false` is not stated, because that is already the default.** Declaring it
#: cost 22 bytes a tool to repeat what the absence of :data:`READS` says — 132 bytes of the 591
#: this addition first measured, spent on nothing. A byte in a schema is context every session
#: carries, so an annotation that changes no client's behaviour is exactly the fat §21.2 asks to
#: be read for before the cap moves.
ADDS = {"destructiveHint": False}

#: Routes ``call_api`` will not reach, and what to do instead — decision `#484`.
#:
#: **Two reasons, and the second was added by `#927`'s H-7.**
#:
#: The first three are *consequential, no undo, and the safety is a confirmation step*. The CLI
#: half of each counts what will change and asks before doing it, which is not a shape a tool
#: call has today.
#:
#: The last two **return a live credential in readable form**, and a tool result is text in a
#: model's context: `POST /v1/tokens` answers with the secret, which exists nowhere else ever,
#: and `POST /v1/login-links` with a working sign-in URL that takes a `username`, so it can be
#: minted *for somebody else*. Not an escalation — `_refuse_amplification` correctly stops a
#: credential widening itself — but a disclosure, into the one place this project has no way to
#: revoke: a transcript. `api/mcp.py` argues at length that this transport must refuse browser
#: sessions because it is "driven by an agent reading item text that anybody with a credential
#: may have written"; the same reader must not be handed a credential either.
#:
#: **That second reason is derived rather than remembered.** ``tests/test_reach.py`` asks which
#: routes answer with a view model carrying a live secret and fails when one of them is missing
#: from here, so a third such route cannot be added without this being decided about it.
#:
#: **The written reason used to say a tool call *cannot* express that, and the protocol has
#: retired it** — elicitation is part of revision ``2025-06-18``, which is the one this server
#: negotiates, and ``2026-07-28`` rebuilds it as Multi Round-Trip Requests: a tool returns what
#: it needs and the client retries with the answer, which is exactly "count, ask, then act". So
#: the entries stand on two reasons that *are* true — support is uneven across the clients agents
#: actually run in, and these three fire perhaps once a month — and each carries its expiry:
#: **delete an entry when a confirmation round-trip is dependable in the clients that matter.**
#:
#: Read by ``tests/test_reach.py`` as well, so there is one definition and two readers.
#:
#: **The third element is one command and nothing else** (`#497`). It carried a clause —
#: "subroutine init, or 'workspace create'" — and the refusal wraps it in quotes, so it rendered
#: as ``Run 'subroutine init, or 'workspace create'' instead``. Prose in the data reads as a typo
#: in the product's own voice, on the one message whose job is handing somebody something to run.
#: **Route templates, not regexes** (`#528`). These were `$`-anchored patterns matched against
#: the caller's raw path string, and three ordinary respellings walked through all of them —
#: `?x=1` fell outside the anchor, `/v1/../v1/workspaces` was resolved by httpx after the check,
#: and `%77` was decoded by the server after it. Each created a workspace. The one entry that
#: held did so by accident, because `[^/]+` happens to swallow a query string.
#:
#: A template is the same thing the application registers, matched by the same function
#: `routing.check` uses — which `tests/test_api_routing.py` holds to the real framework by
#: putting real requests through a real application. And because it is a template rather than a
#: pattern, it can be *checked against the routes that exist*, so renaming a route cannot
#: silently disarm the entry that names it.
DENIED: tuple[tuple[str, str, str], ...] = (
	("POST", "/v1/workspaces", "subroutine workspace create"),
	("PATCH", "/v1/workspaces/{id_or_slug}", "subroutine workspace rename"),
	("POST", "/v1/projects/{id_or_key:path}/move", "subroutine project move"),
	("POST", "/v1/tokens", "subroutine token create"),
	("POST", "/v1/login-links", "subroutine login link"),
)

#: The view models that carry a credential somebody could use, at the one moment it is readable.
#:
#: Named here so :data:`DENIED` can be checked against the routes rather than against a memory
#: of which ones there are. Both say so in their own docstrings — *"the secret is in the URL and
#: nowhere else in this object"*, *"a credential at the one moment its secret exists in readable
#: form"* — and this is that fact made reachable by a guard.
CARRIES_A_SECRET = (subroutine.views.IssuedToken, subroutine.views.SignInLink)

#: How much of a response is worth returning. **A refusal rather than a truncation**, because a
#: truncated JSON document is unparseable and reads as an answer: the caller gets something
#: shaped like a result and cannot tell. Refusing names the three ways to narrow it, which is
#: also the one place an agent reliably learns §14.10's shaping exists.
MAX_ANSWER = 64 * 1024


def references (
	client: subroutine.clients.base.Client, *, workspace: str | None = None,
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
			also_at="/v1/docs/agent",
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
			also_at="/v1/docs/examples",
		),
		subroutine.mcp.protocol.Resource(
			uri="subroutine://meta",
			name="vocabulary",
			title="What this installation calls things",
			description=(
				"This workspace's status keys, item types, link types and tags, plus what each "
				"listing accepts, the limits and the error codes. Read it before constructing a "
				"request by hand: the keys are renameable, so 'done' may be called something "
				"else here."
			),
			mime_type="application/json",
			read=lambda: _vocabulary(client, workspace),
			also_at="/v1/meta",
		),
		subroutine.mcp.protocol.Resource(
			uri="subroutine://conventions",
			name="conventions",
			title="What binds you in this workspace",
			description=(
				"Everything in force here, grouped by kind: the decisions taken, the "
				"procedures and shapes specified, the designs settled, and the routes already "
				"tried and abandoned. Written by the people and agents already working in this "
				"workspace, and binding on the next one. Read it before your first write."
			),
			mime_type="text/markdown",
			read=lambda: _conventions(client, workspace),
			# **Wider than the filters it stands in for, and deliberately.** A client without
			# resources cannot be handed one URL per governing type without being handed the
			# type list too, which is the thing this resource exists to derive. One request
			# that over-returns is honest; four hardcoded ones would be the defect `#1036`
			# fixed, restored in a signpost.
			#
			# **And it carried no status either, which is the same argument one field along**
			# (`#1075`'s sibling, `#1076`). This said `?status=active`, and a status key is
			# renameable where its category is not (§5.5) — sending that literal turned the
			# whole resource into *there is no document status called 'active' here*. A
			# signpost naming it was the same defect on a surface where nothing would refuse
			# it: the URL simply answers about a status the reader does not have.
			#
			# It narrows by *category* now (`#1087`), which is fixed, so this points at the
			# same question the resource answers rather than at every document there is.
			also_at="/v1/documents?status_category=current",
		),
	]


#: The four sections of ``/v1/meta`` that belong to a *workspace* rather than to the instance.
#: Everything else it reports — the listings and their filters, the grammars, the limits, the
#: error codes — is the same whichever workspace you are in, which is what makes `#496`'s answer
#: a subtraction rather than a refusal.
PER_WORKSPACE = ("statuses", "item_types", "link_types", "tags")


def _unbound (meta: subroutine.views.Meta) -> list[str]:
	"""Return the workspaces a caller must choose between, or nothing when there is no choice.

	**A resource cannot be asked again with an argument**, which is the whole of `#496`. The
	tools beside it may reasonably answer an ambiguous request by refusing and inviting a second
	call naming a workspace; these documents take no arguments, so for them "say which" is
	advice the reader cannot act on. Both therefore have to detect the condition themselves and
	answer it in their own content.

	Asked of a :class:`~subroutine.views.Meta` already in hand rather than fetching one, so the
	resource that has it pays nothing — ``/v1/meta`` is the largest response this server makes
	and a second copy of it to answer a yes-or-no question is the cost that would have made
	this check something to be careful about using.
	"""

	if meta.workspace is not None or len(meta.workspaces) < 2:
		return []

	return [one.slug for one in meta.workspaces]


def _choose_a_workspace (names: typing.Sequence[str]) -> str:
	"""Return what to do about it, in the terms of a reader with no arguments to pass."""

	return (
		f"This installation has more than one workspace — {', '.join(names)} — and this "
		f"session is not bound to one, so nothing here can tell which you mean. Ask for one by "
		f"name with subroutine_call_api, for example GET /v1/meta?workspace_id={names[0]}; or "
		f"ask the person running this session to set the plugin's 'workspace' setting, which "
		f"binds every call including these documents."
	)


def _vocabulary (client: subroutine.clients.base.Client, workspace: str | None) -> str:
	"""Return this installation's vocabulary as JSON — `#486`.

	**Bound to the session's workspace, not to whatever is sole.** ``catalogue`` takes the same
	setting and treats it as a default a caller may override; a resource has no arguments to
	override *with*, so this is the only place the binding can be applied — and a resource
	reporting a different workspace's keys from the tools beside it would be worse than not
	publishing them.

	Serialised through pydantic rather than by hand so that a field added to
	:class:`subroutine.views.Meta` appears here without being listed anywhere twice.

	**With no workspace chosen the four per-workspace sections are removed, not emptied**
	(`#496`). ``/v1/meta`` answers an unbound request with `200` and empty sections, which is
	right over HTTP because the caller can read ``workspaces`` and ask again — but here it made
	the one document whose job is preventing a guess say this workspace has no statuses, no
	types, no link types and no tags. That is `api/meta.py`'s own warning, one surface along:
	*discover by being refused* inverted into *discover by being told something false*.

	Removing them rather than refusing the read, because the rest of this document is
	instance-wide and correct — the listings, the grammars, the limits and the error codes are
	most of it and are exactly what "before constructing a request by hand" means. An absent key
	makes no claim; an empty one does.
	"""

	meta = client.meta(workspace=workspace)
	names = _unbound(meta)

	if not names:
		return meta.model_dump_json(indent=1)

	payload = meta.model_dump(mode="json")
	published = {key: value for key, value in payload.items() if key not in PER_WORKSPACE}
	published["vocabulary_not_shown"] = _choose_a_workspace(names)

	return json.dumps(published, indent=1)


def _conventions (client: subroutine.clients.base.Client, workspace: str | None) -> str:
	"""Return what is in force in this workspace, as a readable index — `#506`, `#1036`.

	**The problem it closes, measured on this project's own instance**: 57 governing documents
	open, and the one file a session is guaranteed to read named 24 of them. Ten decisions were
	reachable only by searching, and nothing prompted a search — so the rules an agent is
	expected to follow arrived, if at all, because somebody happened to restate them somewhere
	else. Decision `#499` one level up: the channel that is guaranteed must name every channel
	that is not.

	**A mechanism rather than a list, because these instructions ship with the program.** The
	server instructions are identical on every installation, so they cannot say "read `#506`" —
	a ref belongs to one instance. They name this resource, and the resource asks *this*
	workspace what it has decided. `#486`'s shape exactly, applied to conclusions rather than
	to vocabulary.

	**Titles and refs, never bodies.** §6.14 makes a decision's title state its conclusion, so
	the index is readable on its own and an agent fetches only the one it needs — which is the
	whole of §14's context economy. A resource that inlined 26 documents would be the thing it
	is trying to prevent.

	**Grouped by type, and it asks whether a document is in force rather than what type it
	is** (`#1036`). Asking ``type=decision`` excluded six governing documents on this project's
	own instance, the release procedure and the accountability model among them, with nothing
	wrong with how any of them was written. The grouping is what makes the extra entries
	informative rather than noise: *we decided this* and *the specification says this* and
	*this route is closed* are different obligations.

	**Everything in force is listed, and it is curated by superseding rather than truncated**
	(Simon, 2026-08-20). An index of what binds you cannot honestly omit: an agent held to ten
	rules it was never shown is worse off than one reading a long list. If it grows
	uncomfortable the answer is to supersede what no longer applies, which is the product's own
	mechanism, rather than to pick a number.
	"""

	meta = client.meta(workspace=workspace)
	names = _unbound(meta)

	if names:
		# **It refused, where the vocabulary resource lied, and neither was usable** (`#496`).
		# Left alone this raised the ordinary ambiguity refusal — whose remedy is "pass
		# 'workspace_id'", an argument a resource has no way to pass. Answered here instead, in
		# the same shape as the empty case below, because a document explaining why it is empty
		# is worth more than an error explaining nothing the reader can act on.
		return "\n".join([CONVENTIONS_HEADING, "", _choose_a_workspace(names)])

	lines = [
		CONVENTIONS_HEADING,
		"",
		"Everything below is **in force** here, grouped by what kind of thing it is. The title",
		"states the conclusion, so this index is readable on its own; read the one you need",
		"with `subroutine_show`, by its number.",
	]

	total = 0

	for kind in subroutine.domain.documents.GOVERNING:
		section, held = _governing(client, meta, workspace, kind)

		lines += section
		total += held

	if not total:
		# **A resource with nothing in it must say why**, or it reads as "there are no rules
		# here" — which is a claim, and a false one on any instance that has been used. `#496`
		# is the same failure on the vocabulary resource, found by a stranger's agent meeting
		# an unset workspace.
		#
		# **Asked of every governing type before this is reached** (`#590`, widened by `#1036`).
		# The version that returned as soon as the decisions came back empty made every other
		# section reachable only through that one, so a workspace that had closed a route off
		# without marking a decision in force was told nothing at all.
		lines += [
			"",
			"Nothing is marked as in force here yet, which is not the same as nothing having",
			"been decided. A document written before this workspace started marking them, or",
			"one still being drafted, will not appear.",
			"",
			"`subroutine_list` with a `type` shows every document of that kind whatever its",
			"status, and `subroutine_document` records a new one.",
		]

		return "\n".join(lines)

	lines += [
		"",
		f"{total} in force. Findings and notes are not listed here: they describe rather than",
		"bind, and `subroutine_list` with a `type` finds those. A code review's *Not issues*",
		"section is worth reading before re-raising something it already cleared.",
	]

	return "\n".join(lines)


#: The one heading this resource writes above everything else, named so the ambiguous-workspace
#: answer and the ordinary one cannot drift apart — and so a guard can state what a heading in
#: this document is allowed to be without repeating the string (`#927`'s H-8).
CONVENTIONS_HEADING = "# What binds you in this workspace"


def _governing (
	client: subroutine.clients.base.Client,
	meta: subroutine.views.Meta,
	workspace: str | None,
	kind: subroutine.domain.documents.Governing,
) -> tuple[list[str], int]:
	"""Return one type's section of the conventions index, and how many it lists.

	**Nothing here names a type or a status**, which is the whole of `#1036`: the types come
	from :data:`~subroutine.domain.documents.GOVERNING` and the statuses from this workspace's
	own vocabulary, so removing a type from the set removes its section, and renaming ``active``
	leaves the index populated where it used to empty it.

	Silent when a type has nothing in force, rather than carrying a heading saying so. A
	workspace that has never written a dead end does not need a section on every read to tell
	it that — and the closing count says how many the index holds either way.
	"""

	# **One request naming the *category*, which is what `#1087` built** (`#925`). This used to
	# read `/v1/meta`, filter its statuses by category and send the keys back one call at a
	# time — a copy of a rule the server should be answering, and it existed only because
	# `GET /v1/documents` took a renameable key and nothing else. The dedupe that went with it
	# is gone too: a status belongs to one category, so one call cannot return a row twice.
	listed = client.documents(
		workspace=workspace,
		type=kind.key,
		status_category=subroutine.domain.documents.CURRENT_CATEGORY,
		limit=meta.limits.max_page_size,
	)
	found = list(listed)
	cut = listed.has_more

	if not found:
		return [], 0

	# Ref descending is the same ordering as newest-first — a ref is allocated in creation
	# order within a workspace (§6.2) — and it stays deterministic where ``created_at`` would
	# not, because two documents written in one transaction share an instant.
	found.sort(key=lambda document: document.ref, reverse=True)

	section = [
		"",
		f"## {kind.heading}",
		"",
		kind.obliges,
		"",
		*[f"- **#{one.ref}** — {_on_one_line(one.title)}" for one in found],
	]

	if cut:
		# **Asked rather than inferred from a count** (`#1075`). This read
		# `if len(found) >= bound`, on the reasoning that "every client listing returns a bare
		# list and discards the server's own `has_more`" — which stopped being true in `#1037`,
		# the commit that gave `Listing` the flag. The inference was wrong in both directions
		# while it lasted: `found` is merged across every in-force status, so two statuses at
		# half a page each tripped it with no page full, and one page of exactly `bound` tripped
		# it with nothing behind it.
		#
		# Named in a comment and not in the text below: an item ref belongs to one instance and
		# this string is served by every one.
		section += [
			"",
			"That is a full page, so there may be more of these than are listed. "
			f"`subroutine_list` with `type={kind.key}` shows every one, whatever its status.",
		]

	return section, len(found)


def _on_one_line (title: str) -> str:
	"""Return a title that cannot break out of the list item it is rendered into.

	`#927`'s H-8. ``domain.text.fit`` keeps a title on one line as it is written now, which is
	where that belongs — but **normalising writes does nothing for what is already stored**,
	and this resource is the one an agent is told binds it. A row saved before that change,
	or by any future path that reaches the column another way, still has to render safely.

	Whitespace only, deliberately. Escaping Markdown here would turn every legitimate ``**`` or
	backtick in a decision's title into visible punctuation, which makes the index harder to
	read in order to defend against something the line structure already prevents: what makes
	an injected heading indistinguishable from this document's own prose is that it starts a
	*line*, and inside one there is nothing to be confused for.
	"""

	return " ".join(title.split())


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
				"List open items — tasks and documents — from the backlog. Newest first; "
				"order='-priority_score' is what to work on next, ranking assessed items "
				"above half-assessed above unranked."
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
						"description": (
							"The agenda: overdue, today, in progress, upcoming, next."
						),
					},
					"filter": DATE_FILTER,
					"assignee": {
						"type": "string",
						"description": "Only what is assigned to somebody. 'me' is you.",
					},
					"workspace": WORKSPACE,
				},
			},
			call=lambda arguments: _listed(client, arguments),
			annotations=READS,
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_search",
			title="Search",
			description="Find items by their words. Tasks and documents both.",
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
			annotations=READS,
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
					"ref": {"type": A_REF, "description": "The item's number."},
					"history": {"type": "boolean", "description": "Every change, newest first."},
					# **`#849`. A cap is only defensible together with a way to read the rest.**
					# The note this tool prints when it cuts says which character it stopped at,
					# so continuing is copying a number rather than computing one.
					"from": {
						"type": "integer",
						"description": "Continue a cut body from this character.",
					},
					"workspace": WORKSPACE,
				},
				"required": ["ref"],
			},
			call=lambda arguments: _shown(client, arguments),
			annotations=READS,
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_add",
			title="Add a task",
			description=(
				# **`repeats` named, rather than a `repeat` argument** (`#94`). The grammar
				# already reads one out of the line, so an argument would be a second way to
				# say what `subroutine_add` exists to say — and this list is the only place an
				# agent learns what the line carries, so a capability missing from it is a
				# capability nobody uses. The phrase is spelled out because a repeat is the one
				# part with no sigil to hint at it.
				#
				# **`+web` rather than `+SR`**: `sr` was this project's own key and was retired
				# on 2026-08-08, so the published example named a project that resolves nowhere
				# — and it matches `subroutine_project`'s own example now.
				"Create a task from one line. Dates, tags, priority, estimate and repeats "
				"('every other tuesday') are parsed out of it: "
				"'Fix the boiler by friday !4/2 ~2h #home +web'. "
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
					# **A parent has no sigil and cannot be said in the line at all**, which is
					# the asymmetry that earns this where `project` does not: `+web` already
					# says where something is filed, and nothing says what it is part of
					# (`#999`). Breaking work up and handing the parts over is the loop `#503`
					# is about, and an agent is half that audience.
					"parent": {
						"type": A_REF,
						"description": "The item this is part of, by its number.",
					},
					"workspace": WORKSPACE,
				},
				"required": ["text"],
			},
			call=lambda arguments: _added(client, arguments),
			annotations=ADDS,
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_comment",
			title="Record what happened",
			description=(
				"Add to an item's record of what happened — what you did, what you found, "
				"what failed. A '#42' in the body is a reference, not a link — "
				"subroutine_show offers the link where one fits. For a conclusion the next "
				"session needs, write a document instead. Pass remove=true with words from a "
				"comment to take it back out."
			),
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": A_REF, "description": "The item's number."},
					"body": {"type": "string", "description": "What happened."},
					"remove": {"type": "boolean", "description": "Withdraw it instead."},
					"workspace": WORKSPACE,
				},
				"required": ["ref", "body"],
			},
			call=lambda arguments: _remarked(client, arguments),
			annotations=ADDS,
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_document",
			title="Write or revise a document",
			description=(
				"Record a conclusion the next session needs — a decision, a finding, a "
				"design, a dead end. A comment is what happened; a document is what you "
				"concluded. A '#42' in the body is a reference, not a link — "
				"subroutine_show offers the link where one fits. Pass ref to revise one "
				"rather than writing a second."
			),
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": A_REF, "description": "Revise this one. Omitted stays."},
					"title": {"type": "string", "description": "What it concludes, in one line."},
					"body": {"type": "string", "description": "The reasoning, in Markdown."},
					"type": {
						"type": "string",
						"description": "note, spec, design, decision, finding or dead_end.",
					},
					"project": {"type": "string", "description": "Project key."},
					"tags": {
						"type": "array",
						"items": {"type": "string"},
						"description": "Labels, without the '#'. The same tags tasks use.",
					},
					"workspace": WORKSPACE,
				},
			},
			call=lambda arguments: _wrote(client, arguments),
			annotations=ADDS,
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
					"ref": {"type": A_REF, "description": "The task's number."},
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
					"assignee": {
						"type": "string",
						"description": "Hand it to somebody, by username. '' for nobody.",
					},
					"plan": {"type": "string", "description": "The day to do it. A date or ''."},
					"defer": {
						"type": "string",
						"description": "Hide it until this day, or a time on it. '' to unhide.",
					},
					# **The one half of repeating an agent could not reach at all** (`#94`).
					# `subroutine_add`'s line grammar already *creates* one, so that tool needs
					# no argument — but a line is typed once, and until this there was no way
					# on the curated surface to change how something came round or to stop it.
					# That is the ratchet's test answered precisely (§21.2): what would an
					# agent get wrong without it, rather than is there room.
					#
					# **How it is measured is deliberately not here.** `recurrence_anchor` is a
					# second argument for a choice that matters to habits a person files, and
					# `subroutine_call_api` reaches it — which is what `#484` built the escape
					# hatch for, so the curated surface can stay an opinion.
					"repeat": {
						"type": "string",
						"description": "How often it comes round. '' stops it.",
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
				"Say how two items are related. "
				"'blocks' is what readiness reads — a task with an unfinished blocker is not "
				"listed as ready. Pass remove=true to withdraw the link instead."
			),
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": A_REF, "description": "The item's number."},
					"type": {
						"type": "string",
						# **Not a list of keys** (`#821`). Five are seeded, this named three, and
						# the two it left out — `derives_from` and `documents` — are the pair
						# that join work to the conclusions about it, which is the loop the
						# skill exists to push an agent into. They are renameable per workspace
						# (§5.5), so the list belongs where it is per workspace.
						"description": "A link type key; subroutine://meta lists this workspace's.",
					},
					"other": {"type": A_REF, "description": "The other item's number."},
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
				"permanent and lower case, like web or web-sales. Work is filed under a project "
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
			annotations=ADDS,
			# The only tool here that takes a project and does not call it one: `parent` is
			# where a project key goes, so a refusal about the project it could not find has
			# to name that (`#547`). The `_id` rule cannot derive this one.
			renames={"project": "parent"},
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
			annotations=READS,
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
					"ref": {"type": A_REF, "description": "The task's number."},
					"release": {
						"type": "boolean",
						"description": "Give it back instead of taking it.",
					},
					"workspace": WORKSPACE,
				},
				"required": ["ref"],
			},
			call=lambda arguments: _claimed(client, arguments),
			annotations=ADDS,
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
			annotations=READS,
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_call_api",
			title="Call the API directly",
			description=(
				"For what the tools above do not cover. Prefer them: they carry the "
				"conventions this instance expects, and subroutine_add's line grammar "
				"(!4/2 ~2h #home +web) is not applied to fields you send here. Read "
				"subroutine://meta for this workspace's keys and subroutine://docs/examples "
				"for worked calls. Paths are like '/v1/tasks'."
			),
			schema={
				"type": "object",
				"properties": {
					"method": {"type": "string", "description": "GET, POST, PATCH or DELETE."},
					"path": {"type": "string", "description": "The route, e.g. /v1/projects."},
					"body": {"type": "object", "description": "The JSON body, for a write."},
					"query": {
						"type": "object",
						"description": "Query parameters, as strings.",
					},
				},
				"required": ["method", "path"],
			},
			call=lambda arguments: _called_directly(client, arguments),
		),
		subroutine.mcp.protocol.Tool(
			name="subroutine_done",
			title="Finish a task",
			description="Mark a task complete by its ref number.",
			schema={
				"type": "object",
				"properties": {
					"ref": {"type": A_REF, "description": "The task's number."},
					# **A flag rather than a fifteenth tool.** Both end this occurrence and
					# both bring the next; what differs is which fact gets recorded about the
					# month. The ratchet's test is *what would an agent get wrong without it* —
					# without this it records a skipped repeat as done, and `#574` is about
					# a habit skipped leaving no trace at all.
					"skip": {
						"type": "boolean",
						"description": "Let this one of a repeat go by.",
					},
					"workspace": WORKSPACE,
				},
				"required": ["ref"],
			},
			call=lambda arguments: _completed(client, arguments),
			annotations=ADDS,
		),
	]


def _called_directly (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Make one raw request and report what came back — `#485`.

	**The surface is an opinion and this is the escape hatch**, which is decision `#484` in one
	sentence. The measurement that settled it: of twenty capabilities the tools lacked, thirteen
	were excluded for *budget* rather than by any decision — so this retires a constraint nobody
	chose, rather than adding a thirteenth judgement.

	It widens nothing. The credential is the one the connection already holds, and every check
	the service layer makes for a named tool still runs.
	"""

	method = (_text(arguments, "method") or "").strip().upper()
	path = (_text(arguments, "path") or "").strip()

	if not method or not path:
		raise ValueError("Pass 'method' and 'path', e.g. method='GET', path='/v1/tasks'.")

	if not path.startswith("/"):
		raise ValueError(f"A path starts with '/': {path!r}. Try '/{path.lstrip('/')}'.")

	_refuse_a_denied_route(method, path)

	body = arguments.get("body")
	given = arguments.get("query")
	query = (
		None
		if not isinstance(given, dict)
		else {name: str(value) for name, value in given.items()}
	)

	answer = client.call_api(method=method, path=path, body=body, query=query)

	if len(answer.text) > MAX_ANSWER:
		# **The cap is applied after the call, so the call has already happened** — `#531`.
		# On a `GET` that is merely wasteful. On a write it is `#505`'s shape one layer up:
		# the change is committed and the caller is told this failed, so it repeats it and
		# writes twice. The message is what invites that, so the message says so first.
		advice = (
			" Ask again more narrowly with 'fields' to choose columns, 'limit' to take fewer "
			"rows, or format=compact — see subroutine://meta for what this listing accepts."
			if method in subroutine.clients.base.READING_VERBS
			else " Whatever it changed is already changed, so sending it again would change "
			"things twice rather than report them once. Read the result back instead."
		)

		raise ValueError(
			f"The request reached the instance and it answered {answer.status}, but the answer "
			f"is {len(answer.text) // 1024} KB — more context than it is worth spending, so it "
			f"is not being reported.{advice}"
		)

	# The status is reported rather than folded into the text: a caller that cannot tell 201
	# from 200, or 404 from an empty list, has to infer it from prose written for a person.
	return f"{answer.status} {answer.text}" if answer.text else str(answer.status)


def _readings (path: str) -> set[str]:
	"""Return every path this request could arrive at the router as — `#528`.

	**More than one, because the stack normalises in more than one place and not in one order.**
	httpx resolves dot segments when it merges a path against a base URL, *before* anything is
	sent; the server percent-decodes, *after*. So `/v1/../v1/x` is resolved and then decoded,
	while `/v1/%2e%2e/v1/x` is decoded and then not resolved — and a check that picked one order
	would be blind to the other.

	So the readings are generated and the caller refuses if **any** of them names a denied route.
	Over-refusing is the safe direction here: the cost is an agent being told to use the command
	line for something it could have done anyway, and the cost the other way is the thing this
	exists to prevent happening without anybody being asked.
	"""

	# The query and the fragment are not part of what the router matches, and leaving them on
	# is what let `?x=1` walk past an anchored pattern.
	bare = path.split("#", 1)[0].split("?", 1)[0]
	found = {bare}

	for candidate in (bare, urllib.parse.unquote(bare)):
		# `normpath` resolves `.` and `..` and collapses repeated slashes. It also strips a
		# trailing slash, which the router treats as the same route anyway.
		#
		# **Except a leading `//`, which POSIX says to keep and `normpath` therefore keeps.**
		# `//v1/workspaces` is a 404 from Starlette and *is* the route once anything in front
		# collapses it — nginx does, and this instance is served through a proxy. So the leading
		# slashes are collapsed by hand rather than left to a function that is documented not to.
		resolved = posixpath.normpath("/" + candidate.lstrip("/"))
		found.update({candidate, resolved, urllib.parse.unquote(resolved)})

	return {reading for reading in found if reading.startswith("/")}


def _refuse_a_denied_route (method: str, path: str) -> None:
	"""Refuse the three routes decision `#484` keeps off this surface.

	**Named alternatives, never a dead end.** A refusal that only says "not here" strands an
	agent mid-task; these three exist at a terminal, and saying which command is the difference
	between a wall and a hand-off.

	Matched with ``routing._matches``, which is what decides whether a path template covers a
	path everywhere else in this application — so there is one answer to "does this route match"
	rather than a second one written here, which is what `#528` was.
	"""

	readings = _readings(path)

	for verb, template, instead in DENIED:
		if method != verb:
			continue

		if any(subroutine.addressing.matches(template, reading) for reading in readings):
			raise ValueError(
				f"{method} {path} is deliberately not reachable from here: it is consequential, "
				f"it cannot be undone, and the command line asks before doing it. Run "
				f"'{instead}' instead, or ask the person you answer to."
			)


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

	# **The account's zone, and the same clock the feed reads** (`#1185`, `#1091`). This was a
	# bare ``.isoformat()``, so a lease printed in UTC beside a feed printed in the account's:
	# on an instance an hour off, a claim taken at 12:11 read as having expired *before* the
	# events that renewed it. An expiry is the one moment on this surface an agent is asked to
	# reason about, so it is the one that could least afford its own clock.
	until = ""

	if held.claim_expires_at is not None:
		zone = subroutine.domain.dates.zone(_account_zone(client, workspace))
		until = f", until {held.claim_expires_at.astimezone(zone):%d %b %H:%M}"

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
				# **Described rather than listed** (`#703`), and this is the surface it cost
				# something on: an agent here read `task:write`, found no `document:*`, and
				# concluded it could not write up what it had concluded — which is the one
				# thing the skill spends most of its words persuading it to do.
				f"  may: {', '.join(subroutine.permissions.described(workspace.permissions))}"
				# Anything short of everything (`#717`) — an owner holding all seventeen is the
				# one case where the list says nothing, and it was the only case being served.
				if subroutine.permissions.worth_listing(workspace.permissions)
				else ""
			)
			for workspace in me.workspaces
		)

	# **The early return this used to take is gone, deliberately** (`#381`). A credential that
	# reaches no workspace is the single most likely reason somebody asks this question, and
	# it was the one branch that would have answered without saying which versions were in
	# play — the answer missing from exactly the case that needs it.
	# **Whether these tools can see the caller's machine at all** (`#564`). Since `#539` they
	# run wherever the *instance* runs: in the relay's own process for a local connection —
	# which is the process the plugin started, so its environment is the caller's — and on a
	# server for a remote one, where the caller is on another machine entirely.
	#
	# `installations.plugin()` reads `CLAUDE_PLUGIN_ROOT` out of the environment, so **a value
	# here is proof of standing in the caller's process**. A null is not proof of the opposite,
	# which is why this errs the way it does: reporting `installations.program()` regardless
	# gave *"Program X, instance X"* with X the instance twice and one of them labelled as the
	# caller's, and an agent read that as "no version problem". Saying nothing is a worse answer
	# than saying nothing *confidently*.
	#
	# The cost is a hand-started `subroutine mcp` on a local connection with no plugin, which is
	# beside the caller and is told this cannot be seen. Over-cautious in the safe direction,
	# and the only way to do better is for the caller to send what it is running — the header
	# `#564` records, which this does not build.
	beside_the_caller = subroutine.installations.plugin()

	lines.append("")
	lines.extend(
		subroutine.views.versions(
			me,
			program=(
				None if beside_the_caller is None else subroutine.installations.program()
			),
			plugin=beside_the_caller,
		)
	)

	# **`machine` follows `program`'s own test** (`#1089`, `#564`). Where the caller's
	# installation is not visible from here the process is the *server*, so reading its zone
	# would compare an account against a machine nobody is sitting at and label it the
	# caller's — which is the direction that reassures, and therefore the worst one to get
	# wrong.
	lines.extend(
		subroutine.views.zones(
			me,
			machine=(
				None
				if beside_the_caller is None
				else subroutine.config.system_timezone()
			),
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

	# **Said whether or not anything changed, and that is the point** (`#1085`). A credential
	# narrowed away from one of the three kinds used to be refused this feed outright; it now
	# gets the kinds it may read — so *nothing has changed* and *I am not shown that* are two
	# answers that would otherwise be the same sentence. Stated positively, per Simon's
	# decision of 2026-08-22: what this covers, rather than what was left out, which needs the
	# reader to know what exists before it means anything.
	#
	# Silent on an instance that does not publish it, which is one a release behind — that is
	# "did not say", and inventing a list here would be a claim this surface cannot support.
	coverage = f"This feed covers {_kinds_named(events.covers)}." if events.covers else ""

	if not events:
		return " ".join(filter(None, ["Nothing has changed.", coverage]))

	# **The account's zone, not this process's** (`#1091`). This was a bare ``.astimezone()``,
	# which is the *server's* ``/etc/localtime`` for every relayed connection since `#539` —
	# nobody's zone, and here it decides the day as well as the time.
	zone = subroutine.domain.dates.zone(
		_account_zone(client, _text(arguments, "workspace"))
	)
	lines = [
		f"{event.seq}  {event.created_at.astimezone(zone):%d %b %H:%M}  {_happened(event)}  "
		f"{_named(event)}"
		for event in events
	]
	footer = f"Resume with since={events[-1].seq}."

	return "\n".join([*lines, " ".join(filter(None, [footer, coverage]))])


def _kinds_named (kinds: typing.Sequence[str]) -> str:
	"""Say which kinds a feed carries, in words rather than as a vocabulary — `#1085`.

	Pluralised, because the sentence is about a class of thing and *covers task and document*
	reads as two particular ones. Joined here rather than through a shared helper: this
	repository already has four spellings of *join a list into a sentence* and they differ on
	purpose — "and" against "or", Oxford comma against none — so a fifth caller of one of them
	would have to accept somebody else's punctuation.
	"""

	said = [f"{kind}s" for kind in kinds]

	if len(said) < 2:
		return "".join(said)

	return f"{', '.join(said[:-1])} and {said[-1]}"


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
	# five headings for what is usually five rows is paying for the headings.
	if arguments.get("today"):
		agenda = client.agenda(**_agenda_asked(arguments))

		# **Every bucket, and each row saying which it is in** — Simon's decision of
		# 2026-08-18, decision `#989`. Three of the five reached an agent until then: the
		# argument for dropping `unscheduled` was that it is the terminal's filler and none of
		# it is *on today*, and the measurement that reversed it is that **11 of 170 open tasks
		# here are dated**. So on an ordinary day an agent was told *"Nothing on today."* while
		# the browser showed twenty ranked items — the divergence is the common case, not the
		# edge. `in_progress` was missing by omission rather than by choice.
		#
		# **The label is what earns the bytes.** These rows carried no bucket at all, so
		# overdue was distinguishable from due-today only by comparing dates and a backlog row
		# would be distinguishable from a commitment only by the absence of a deadline. Flat
		# parity without labels would be worse than the drop it replaced.
		moment = subroutine.db.types.utcnow()
		every = [
			_line(task, now=moment, bucket=bucket)
			for bucket in subroutine.views.AGENDA_BUCKETS
			for task in getattr(agenda, bucket)
		]

		# **`limit` narrows an agenda only when it is asked for**, unlike the backlog below
		# where twenty is a sensible page. A day is a structure rather than a page: silently
		# returning the first twenty rows of it would drop whichever bucket came last, which
		# is the drop this decision just reversed. It was computed on this branch and read by
		# nothing at all until now — `#251`'s shape, a declared argument that is a no-op.
		rows = every if arguments.get("limit") is None else every[:limit]

		# **What is held back is said, never simply absent** (§12.2a, and `#888`'s condition on
		# any cap here). Three things can hide a row — this limit, the agenda's own cap on
		# undated work, and the look-ahead's edge (`#997`) — and an agent that cannot tell a
		# short day from a truncated one will act on the wrong one.
		#
		# **Two counts rather than one number**, because the remedies differ: a larger limit
		# reaches the first, and only a listing reaches the second. One total would be a
		# figure with no action attached to it.
		hidden = (
			len(every) - len(rows) + agenda.unscheduled_total - len(agenda.unscheduled)
		)

		if hidden > 0:
			rows = [*rows, f"{hidden} more not shown. Raise limit, or list ready=true."]

		if agenda.later_total > 0:
			rows = [
				*rows,
				f"{agenda.later_total} dated further out. "
				f"List with filter due_at.gte=today, order due_at.",
			]

		return "\n".join(rows) if rows else "Nothing on today."

	project = _text(arguments, "project")

	# §9.6's date comparisons (`#815`). Refused by the far end rather than checked here, like
	# every other vocabulary an agent sends — so a misspelled field is named once, in the one
	# place that holds the registry.
	filters = _filters(arguments)

	# **The read half of delegation** (`#1114`). This surface could hand work to somebody and
	# not ask what had been handed to it: `subroutine_update` takes an assignee and nothing
	# here took one, so an agent could delegate and could not be delegated to. Every layer
	# beneath it has answered this since `#501`; the gap moved up to the one surface nobody
	# re-measured.
	assignee = _text(arguments, "assignee")

	tasks = client.tasks(
		workspace=workspace,
		project=project,
		limit=limit,
		order=_text(arguments, "order"),
		ready=ready,
		q=query,
		assignee=assignee,
		filters=filters,
	)

	# **`limit` bounds the answer, not each kind**, which is what the caller's budget means —
	# asking for five and receiving five tasks followed by five documents spends it twice.
	#
	# **Each kind is asked at the full limit and the merged answer is cut to it** (`#1010`).
	# This used to ask for `limit - len(tasks)`, which is the same sentence read as an
	# allocation rather than as a cut: tasks were fetched first and documents got what was
	# left, so at a small limit a document ranking above every task was **absent** rather than
	# late. `cli/personal._listing` states the rule and gets it right — *"twenty documents must
	# not be able to push every task off a page"* — and an agent did that in reverse. Measured
	# on the served instance before the fix, one query at `limit=4`: the terminal answered
	# `989 906 1001 1010` and an agent `525 440 904 1001`, one row in four shared.
	#
	# **Never documents when `ready` was asked for.** §6.14 says a document is not scheduled
	# and nothing blocks one, so every specification and decision in the instance would report
	# as ready — true, useless, and enough of them to bury the tasks the caller asked about.
	# **A date a document has not got means *no* documents, never all of them** (`#815`). A
	# document is not scheduled (§6.14), so *what did I complete yesterday* is a question about
	# tasks — and a second call that dropped the filter it could not honour would answer it by
	# adding every decision in the workspace.
	documents = (
		client.documents(
			workspace=workspace,
			project=project,
			limit=limit,
			q=query,
			filters=filters,
		)
		if not ready and _asks_only_of_documents(filters)
		else []
	)

	# **The order the server put each of them in, read off the rows** — `ordering.merge_order`,
	# which the terminal and the browser have both read since `#875`/`#878` and this surface
	# could not reach. Concatenating two ranked pages is two sorted runs end to end, so a
	# document that answered best appeared below every task that merely mentioned the words.
	found = [*tasks, *documents]
	ordered = subroutine.domain.ordering.merged(
		found,
		key=lambda row: row,
		order=subroutine.domain.ordering.merge_order(
			_text(arguments, "order"),
			subroutine.domain.ordering.requested(
				_text(arguments, "order"),
				allowed=subroutine.domain.ordering.TASK_FIELDS,
				default=subroutine.domain.ordering.DEFAULT_TASK_ORDER,
			),
			ranked=any(
				getattr(row, subroutine.domain.ordering.RELEVANCE, None) is not None
				for row in found
			),
		),
	)

	moment = subroutine.db.types.utcnow()
	rows = [_line(item, now=moment) for item in ordered[:limit]]

	if not rows:
		return "Nothing open."

	# **What is held back is said, never simply absent** — docs/design.md §12.2a, and this
	# branch was the one place here that did not (`#1071`). The agenda ten lines above says
	# *"N more not shown"*; this returned `ordered[:limit]` and nothing, so an agent asking for
	# twenty received twenty and had no way to tell whether that was the answer or the cut.
	#
	# **Two ways to be short and both count.** The merge itself may have trimmed rows this
	# already holds, or either kind may have said there were more behind it — `Listing.has_more`
	# was added for exactly this in `#1037` and was read by nothing on this surface.
	#
	# **No number, because there honestly is not one.** `has_more` is a flag; counting would
	# cost a second full scan per kind, which is the trade §8.4's `include_total` already
	# declines. Saying *more* without a figure is the same answer the CLI's *…and more* gives.
	if len(ordered) > limit or tasks.has_more or getattr(documents, "has_more", False):
		rows.append("More matched than are shown. Raise limit, or narrow with project or filter.")

	return "\n".join(rows)


def _filters (arguments: dict[str, typing.Any]) -> dict[str, str]:
	"""Read the ``filter`` argument, refusing values the declared schema does not allow.

	**Only what the generic check cannot reach**, which was measured rather than assumed. `#549`
	made ``protocol._mistyped`` refuse an argument whose value does not match its declared
	``type``, so ``filter="created_at.gte=today"`` is already turned down by name before this
	runs — and the first version of this function checked that again. Found by falsifying: the
	mutation that removed the check *passed*, which is a finding about the code rather than
	about the test.

	What that check does not do is recurse: it reads the property's own ``type`` and knows
	nothing about ``additionalProperties``. So ``{"created_at.gte": 5}`` reaches here, and this
	is the only place that can refuse it.

	The *names* are not checked here either. Those belong to ``domain/filtering``'s registry,
	which lives on the instance — so a misspelled field is refused once, by the side that knows,
	and a client one release behind can still ask a question its instance understands.
	"""

	given = arguments.get("filter")

	if not isinstance(given, dict):
		return {}

	if not all(
		isinstance(name, str) and isinstance(value, str) for name, value in given.items()
	):
		raise subroutine.errors.ValidationError(
			"'filter' takes a field.operator and a value, both written as text.",
			errors=[
				subroutine.errors.FieldError(
					field="filter",
					code="invalid_field_value",
					message="One of the entries in 'filter' was not a pair of strings.",
					hint="Write it as {\"created_at.gte\": \"yesterday\"}.",
				)
			],
		)

	return dict(given)


def _asks_only_of_documents (filters: dict[str, str]) -> bool:
	"""Report whether every filter names a field a document actually has — `#815`.

	The same rule the CLI applies, and here for the same reason: a second call that dropped a
	filter it could not honour would make a narrowed list *longer*.
	"""

	return all(
		name.partition(subroutine.domain.filtering.SEPARATOR)[0]
		in subroutine.domain.filtering.DOCUMENT_FILTERS
		for name in filters
	)


def _agenda_asked (arguments: dict[str, typing.Any]) -> dict[str, typing.Any]:
	"""Return what an agent's agenda asks the instance for.

	**Lifted out of the branch so that something other than a model can ask it** (`#992`).
	Three surfaces build this request — here, `cli/personal.agenda_asked` and
	`agendaRequest()` in `app.js` — and nothing compared them, so they asked three different
	questions of one function and every difference reached a reader as a different answer to
	*what should I work on next*.

	**The look-ahead is asked for rather than left to default** (`#991`). ``GET /v1/agenda``
	omits ``upcoming`` unless asked, deliberately, so a client can reason about the window it
	gets — and this asked for nothing, so the bucket was always empty and a deadline on Friday
	was on no agenda an agent could see.

	**Not a default on ``clients/base.Client.agenda`` instead**, which is where `#991` proposed
	putting it. Two reasons, both found by building: the browser is not a client and reaches
	the endpoint directly, so a client-side default could never be the shared one; and ``None``
	already means *omit the bucket* on the wire, so giving it a value would leave no way to
	say that. What the three surfaces share is the *number*, and they name it each.
	"""

	return {
		"workspace": _text(arguments, "workspace"),
		"horizon_days": subroutine.domain.agenda.DEFAULT_HORIZON_DAYS,
	}


def _day_of (
	instant: datetime.datetime,
	item: subroutine.views.Task | subroutine.views.Document,
) -> str:
	"""Return the day an instant fell on, **where the item lives** (`#1064`).

	The terminal's ``_render_date`` in the same words, so `#674`'s guard is comparing two
	renderings that agree rather than two that happen to name the same fields. Both go through
	:func:`subroutine.domain.schedule.day_in`, which is where the reason lives.

	A document carries no ``timezone`` — it has no dates of its own to read — and the fallback
	handles it, which is why this takes the item rather than the string.
	"""

	return subroutine.domain.schedule.day_in(
		instant, getattr(item, "timezone", None)
	).isoformat()


def _line (
	item: subroutine.views.Task | subroutine.views.Document,
	*,
	now: datetime.datetime,
	bucket: str | None = None,
) -> str:
	"""Return one item as a line: address, kind, bucket, state, rank, estimate, title.

	Assembled here rather than reusing ``?format=compact``, which is a *terminal* rendering —
	aligned columns with long titles cut short. A model reading a truncated title has been
	given damaged data to save characters it did not need saving.

	``now`` is required rather than read here, so that every row of one page is judged against
	one instant — `#361`'s rule, and the reason is sharper for a *claim* than for a query: two
	rows of one listing disagreeing about whether a lease had run out would be a page that
	contradicts itself.
	"""

	cells = [subroutine.domain.refs.format_ref(item.ref), item.type]

	# **Which section of the day this is, before anything a workspace can rename** (`#991`).
	# An agenda is returned flat, so without this an agent has the rows and not the structure —
	# and `unscheduled` reads exactly like `today` with the deadline left off. Ahead of the
	# status because §5.5 makes that vocabulary a workspace's own: an installation is free to
	# call a status *Today*, and a reader taking the first cell it recognises must not be able
	# to read one as the other.
	if bucket is not None:
		cells.append(bucket)

	if isinstance(item, subroutine.views.Task):
		# **What is already started, and who is holding it** (`#841`). `#705` tells an agent to
		# claim an item and set it `in_progress`; `#777` measured the result and nothing had
		# ever been claimed. This is the read half — the convention asked every agent to
		# announce what it was doing on a surface where no other agent could hear it, so two
		# would pick the same item and the lease that exists to prevent exactly that (§14.11)
		# was invisible to the only readers it was built for.
		#
		# **Both cells cost nothing on a row that has nothing to say**, which is what answers
		# `#819`'s rule that a fact belongs in `show` unless the listing needs it. Measured on
		# this instance: 2 of 172 open tasks carry a status worth printing and 1 carries a live
		# claim. What the other 169 rows pay is zero bytes, and what these three change is
		# which item you should pick — which is the question a listing is being asked.
		#
		# **Tasks only, and that is measured rather than tidy.** `draft` is a document's
		# default and `active` is not, so asking `status_is_news` of a document would put a
		# cell on 111 of this instance's 122 — §12.2a's "a column that says the same thing on
		# every row says nothing", arrived at from the other direction.
		# **`state_is_news_in_a_listing`, not `status_is_news`** (`#874`). The narrower one is
		# for a fact sheet, which prints a completion date beside the status; a row has no date
		# and no room for one, so a finished item here looked exactly like an open one. Found by
		# driving the served instance after `#873` made `search <ref>` return finished work by
		# design — `#815` came back marked `holds up work` and said nothing about being over.
		if subroutine.views.state_is_news_in_a_listing(item):
			cells.append(item.status)

		# **Through `views.holder` rather than off `claimed_by`**, because the view reports an
		# expired lease on purpose and the clock is what makes it stop counting. Of the three
		# claim records on this instance, two had expired: reading the column alone would have
		# been wrong about two rows in three, and wrong in the direction that stops an agent
		# picking up work nobody is doing.
		held = subroutine.views.holder(item, now=now)

		if held is not None:
			# **`claimed by`, matching the `claim` and `release` verbs** (`#1019`, Simon). It
			# was `held by`, and the browser said `x is on it` — one lease with two names on
			# two surfaces, which is `#913`'s defect one fact along. The row's wording is not
			# in §21.2's schema budget, so agreeing cost nothing.
			cells.append(f"claimed by @{held}")

		# **Before the rank, because it changes what the rank means** (`#425`). A default
		# listing put a blocked item above the thing blocking it with nothing to say so, and an
		# agent reading one reported it as "start with #2". `ready=true` filters correctly; the
		# listing an agent gets by asking for the backlog is the one that could not tell it.
		# **Both directions, and `blocked` first** (`#569`, the mirror of `#425`). The report
		# that started this was an agent reading a board: the urgent item was marked and the
		# five-minute errand holding it up was not, so the only thing worth starting looked
		# like the least important row on the page. A cell each rather than one with a
		# precedence — this is a list an agent parses, and a row can be both.
		# **The words come from `views` rather than being written here** (`#913`). This module
		# spelled both of them out while `cli/personal` named the same two, so there were two
		# copies and nothing compared them — and the blocking one had drifted: a card said
		# `holds up work` where the item it opened said `Blocks`.
		if item.blocked:
			cells.append(subroutine.views.BLOCKED_MARK)

		if item.blocking:
			cells.append(subroutine.views.BLOCKING_MARK)

		if item.importance is not None or item.urgency is not None:
			cells.append(f"!{item.importance or '?'}/{item.urgency or '?'}")

		if item.estimate_human is not None:
			cells.append(item.estimate_human)

		# **The day it is planned for, before the day it is wanted by** (`#673`). These are two
		# facts and reporting only the second loses the one the caller just set: an agent that
		# captured "Dentist appointment on monday" was answered with a line saying nothing
		# about Monday, and the only trace was the words having left the title — which is
		# indistinguishable from their never having been read.
		#
		# It matters here more than anywhere because the skill names this line as *the* check:
		# "whatever it read is echoed back, so check that line". An agent doing as it is told
		# learned nothing, and the cost is asymmetric — what it cannot rule out is a day
		# silently set to the wrong one, which nobody discovers until the day has passed.
		#
		# `for` rather than `on`, matching the CLI's own phrase, even though `on` is the word
		# §6.13 actually parses. One product says one thing; whether *both* should say `on` is
		# a question about voice and is `#691`.
		# **In the task's own zone, which is the rule this line did not follow** (`#1064`).
		# These were ``.date()`` on the stored instant, so a Los Angeles deadline read a day
		# late and a London plan a day early — on the line the paragraph above calls *the*
		# check. The check said the wrong day and said it confidently.
		if item.starts_at is not None:
			cells.append(f"for {_day_of(item.starts_at, item)}")

		if item.due_at is not None:
			cells.append(f"due {_day_of(item.due_at, item)}")

		# **On the row, not only in `show`** (`#922`). `_more`'s own argument for carrying it
		# is that a repeat changes what every other fact means — *due Thursday* on something
		# fortnightly is a different statement from *due Thursday* on a one-off — and that
		# reasoning is stronger here, because a listing is what an agent picks work from.
		#
		# **And this line is what a write is answered with**, so without it an agent that set
		# a repeat through `subroutine_update` was told *Changed* and never saw the thing it
		# had changed. That is `#674`'s status defect word for word, on the same surface, two
		# months later.
		#
		# The terminal's row has carried it since the day the CLI learned about repeats; the
		# guard could not see the difference because it compares `_facts` against the *union*
		# of the three agent renderers, and a fact in `_more` alone satisfies a union.
		if item.recurrence_rule is not None:
			cells.append(
				subroutine.domain.recurrence.describe(
					item.recurrence_rule, anchor=item.recurrence_anchor
				)
			)

		# **Who has it** (`#511`). `#493` gave an agent the ability to hand work over and this
		# renderer could not report the result — so the tool that assigns and the tool that
		# reads disagreed about whether anything had happened. The item filed against this
		# said an agent *could* already see it because `assignee_id` is in the JSON; that is
		# true of raw HTTP and not of here, which returns text.
		#
		# The username rather than the id, for the reason the comment renderer gives below: a
		# UUID is thirty-six characters a model cannot resolve without another call.
		if item.assignee:
			cells.append(f"@{item.assignee}")

	# **How much prose it carries, where it is large enough to matter** (`#595`). One document
	# on this instance is 128,083 characters — about 32,000 tokens — and its row here was the
	# same shape as a row for a three-word note. That is roughly ten times the whole tool
	# surface, spent by a reader who had no way of knowing, on the one surface where §13 makes
	# context a first-order cost.
	#
	# In the row rather than in `show`, unlike `#819`'s tags and `#700`'s deletion date, and
	# the difference is the point: this is the fact a caller needs *before* deciding to read,
	# so it is worthless anywhere except the listing it decides from.
	if item.size_bytes is not None and item.size_bytes >= subroutine.domain.text.LARGE_PROSE:
		cells.append(f"{round(item.size_bytes / 1000)}k")

	cells.append(item.title)

	return "  ".join(cells)


def _happened (event: subroutine.views.Event) -> str:
	"""Return one event as a phrase, in the same words the CLI uses.

	Behind an argument rather than always: a history is unbounded where a comment list is
	bounded by what somebody typed, and most items have one event saying they were created.
	Spending that on every ``show`` is the cost §14 exists to weigh.

	**The words are `views.happened`'s** (`#1115`). They were written out here and again in the
	CLI, identically and identically wrong, so every cross-surface comparison passed over a
	history that called thirteen links a conversation.
	"""

	return subroutine.views.happened(event)


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
		raise LookupError(
			f"There is no #{ref} here. Run subroutine_list to see what there is, or "
			f"subroutine_search to look for it by words in its title."
		)

	return document, "document"


def _more (item: subroutine.views.Task | subroutine.views.Document) -> list[str]:
	"""Return the facts ``show`` promises that a listing row leaves out (`#674`).

	Each is something somebody *chose*, which is `_facts`'s rule at the command line and the
	reason this is not simply the whole view: a status nobody set and a project nobody picked
	are the absence of a decision, and reporting them would bury the ones that are news.

	**The status is the one that matters most here**, because this surface is where an agent
	is told to set it. It sends ``update(ref, status='in_progress')``, is answered *Changed*,
	and then no tool in the catalogue would ever say so again — so it cannot tell its own
	write from a write it only thinks it made. Left out when the item is finished, where
	``done <date>`` says it better, and when it is the one everything starts in.

	Dates are ISO here where the command line renders them for a reader, and the project
	carries the ``+`` a capture line uses. Both for `#151`'s reason: what a caller is shown
	should be what it can send back.
	"""

	facts = []

	if subroutine.views.status_is_news(item):
		facts.append(item.status)

	if isinstance(item, subroutine.views.Task):
		# **Reported whether or not it has passed.** A defer is a decision somebody made, and
		# one that has come round is still the answer to why this was not on the list in June.
		if item.snoozed_until is not None:
			facts.append(f"from {_day_of(item.snoozed_until, item)}")

		if item.completed_at is not None:
			facts.append(f"done {_day_of(item.completed_at, item)}")

		# **Both renderings say it, which `#674`'s guard is what made true** (`#94`). It caught
		# this within the hour of the terminal gaining it: a repeat is the fact that most
		# changes what the others mean, because "due Thursday" on something that comes back
		# every fortnight is a different statement from "due Thursday" on a one-off.
		if item.recurrence_rule is not None:
			facts.append(
				subroutine.domain.recurrence.describe(
					item.recurrence_rule, anchor=item.recurrence_anchor
				)
			)

		# **The series, and it matters more here than at the terminal** (`#921`). An agent
		# following ``recurrence_template_ref`` lands on a row with the same title as the
		# occurrence it came from, and nothing else distinguishes them — so without this it can
		# read the rule and act on it as though it were the work. One wording, from
		# `subroutine.views`, because `#674` compares this list against the terminal's.
		#
		# **Read as an attribute inside the task block**, for the reason written out beside its
		# twin in `cli/personal._facts`: `#674`'s guard derives what a rendering shows by
		# scanning ``item.<field>``, so spelling this ``getattr(item, "is_template", False)``
		# put it where the guard could not see it — measured, by deleting this and watching 508
		# tests stay green.
		if item.is_template:
			facts.append(subroutine.views.THE_SERIES)

	# The project only when somebody filed it somewhere. The Inbox is where things go when
	# nobody said, so naming it would be reporting the absence of a decision.
	#
	# **The whole address since `#512`**, and `+` still leads it because a capture line reads
	# one: `+substation/dist` is what `#958` widened the grammar to accept, so what this prints
	# stays a thing the reader can send back.
	if item.project_path and item.project_path.lower() != "inbox":
		facts.append(f"+{item.project_path}")

	# **Last, and it matters more here than at the terminal** (`#700`). A person reading an
	# item in the trash is at least reading it; a model may act on what it read. This is the
	# one fact in the list that is not about a choice — it changes what all the others mean.
	if item.deleted_at is not None:
		facts.append(f"deleted {_day_of(item.deleted_at, item)}")

	return facts


#: How many children ``subroutine_show`` lists. The terminal's own ceiling, and here for the
#: same reason: a depth limit exists and nothing bounds breadth, so an item with four hundred
#: parts should print a number rather than four hundred lines.
MAX_CHILDREN = 50


def _shown (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Return one item in full, with its links and its record."""

	ref = _ref(arguments)
	workspace = _text(arguments, "workspace")
	found, kind = _item(client, ref, workspace)

	# **The zone a comment's day and an event's day are read in** (`#1091`). Both were
	# ``.date()`` on the stored instant, which is UTC, so a comment written at nine in the
	# evening in Auckland was reported as having happened the next day. Resolved once here
	# rather than per line, and it costs nothing: since `#539` these tools run inside the
	# instance, so asking is a local read.
	reading = _account_zone(client, workspace)

	parts = [_line(found, now=subroutine.db.types.utcnow())]

	# **In `show` rather than in `_line`**, on `#819`'s argument: this is the tool that
	# promises *in full*, and a listing row stays as terse as it was for both kinds.
	if more := _more(found):
		parts.append("  ".join(more))

	body = (
		found.description if isinstance(found, subroutine.views.Task) else found.body
	)
	body_at = None

	if body:
		parts.append("")
		body_at = len(parts)
		parts.append(body)

	# **Echoed because this tool accepts them** (`#819`). `#673`'s lesson is quoted in `_line`
	# below and applies here: the skill tells an agent to check the line it gets back, so a
	# surface that takes `tags` and reports none leaves it unable to tell applied from ignored.
	# In `show` rather than `_line`, because this is the tool that promises *in full* — a
	# listing row stays as terse as it was, for both kinds.
	if item_tags := list(found.tags):
		parts.append("")
		parts.append("  ".join(f"#{tag}" for tag in item_tags))

	# **Its parts, which this surface omitted entirely** (`#1117`). A person's `subroutine show`
	# has rendered them since `#84` gave a milestone its model — an item whose blockers are its
	# contents, a feature that is just a parent item — and an agent reading the same item saw
	# nothing at all. On `#57`, whose own body says *"Four sub-items below"*, that reads as
	# *the parts were deleted* rather than as *this surface does not draw them*.
	#
	# **Finished ones included and marked**, like the terminal's: a parent showing two of four
	# children because the other two are done misreports the thing somebody opened it to see.
	#
	# **Only a task**, because only a task has children; a document reaches this with nothing
	# to ask and the request is not made.
	children = (
		client.tasks(
			parent=ref,
			workspace=workspace,
			limit=MAX_CHILDREN,
			include_completed=True,
			order="ref",
		)
		if kind == "task"
		else []
	)

	if children:
		done = sum(1 for child in children if child.completed_at is not None)

		parts.append("")
		parts.append(f"Parts ({done} of {len(children)} done)")
		parts.extend(
			f"#{child.ref}  {child.title}"
			+ ("  (over)" if child.completed_at is not None else "")
			for child in children
		)

	# **What has been checked against this, and it is a record rather than a proof** (`#1121`).
	# Somebody can post an exit code of zero without having run anything, so what it is worth
	# is being durable, attributable and invalidatable — never *verified work*.
	#
	# **The tree, not the clock.** A record naming no tree cannot go out of date and says so,
	# which is a different answer from being current: §1.4 requires a record to be possible
	# from a machine with no checkout, which is most of them.
	#
	# **Only a task**, because only a task is checked.
	recorded = (
		client.verifications(ref=ref, workspace=workspace) if kind == "task" else []
	)

	if recorded:
		parts.append("")
		parts.append(f"Recorded checks ({len(recorded)})")
		parts.extend(
			f"{'passed' if one.passed else 'failed'}  "
			f"{subroutine.views.moment_day(one.ran_at, reading)}  "
			f"{one.summary or ''}"
			+ (f"  (tree {one.tree_hash[:7]})" if one.tree_hash else "  (no tree)")
			for one in recorded
		)

	# **What binds whoever picks this up** (`#1119`) — `subroutine://conventions` narrowed to
	# one item. Placed **first among the sections** rather than last, because it is the one an
	# agent has to read before doing anything and everything below it is what it reads after.
	#
	# **Typed links only** (`#1124` Q2). Filed nearby and mentioned in passing mean *near
	# this*; answering that under this heading is how an agent learns not to trust it.
	binding = client.governing(ref=ref, entity_type=kind, workspace=workspace)

	if binding:
		parts.append("")
		parts.append(f"Read first ({len(binding)})")
		parts.extend(
			f"#{one.document.ref}  {one.document.type or ''}  {one.document.title}"
			for one in binding
		)

	links = client.links(ref=ref, entity_type=kind, workspace=workspace)

	if links:
		# **How much of a milestone is left, and which ends are over** (`#970`). This line said
		# the label, the ref and the title, so an agent asking whether it could start work had
		# to read every blocker in turn — and `subroutine show` has answered it since `#210`,
		# which is the terminal being a version ahead of the surface with no alternative.
		#
		# **Counted over incoming `blocks` alone**, which is the terminal's own rule: a
		# *relates to* has nothing to be N of.
		#
		# **`over` rather than `done`.** `is_complete` is `completed_at is not None`, which
		# invariant 5 makes true for done *and* cancelled — so the obvious word asserts
		# something about half of them that nobody did.
		blockers = [
			# **The category, never the key** — what a relation *is*, never what it is called (decision `#1157`). Comparing `link_type` to the literal `blocks` kept working while `#1156` broke: a workspace that renames the key keeps every label and loses every count.
			link
			for link in links
			if link.link_category == subroutine.domain.readiness.GATING
			and link.direction == "incoming"
		]
		finished = sum(1 for link in blockers if link.other.is_complete)

		parts.append("")

		if blockers:
			parts.append(f"{finished} of {len(blockers)} blockers done")

		parts.extend(
			f"{link.label}  #{link.other.ref}  {link.other.title}"
			+ ("  (over)" if link.other.is_complete else "")
			for link in links
		)

	# **What refers to this** (`#144`), and it is not the same question as what it is linked
	# to. A link is an assertion somebody made; a mention only records that one piece of
	# writing talks about another (§6.15) — so an agent deciding whether something is safe to
	# close needs both, and had neither until now on any surface.
	#
	# **In `show` rather than in a listing row**, on `#819`'s argument: this is the tool that
	# promises *in full*, and the cost is one request per item opened rather than per row.
	referring = client.backlinks(ref=ref, entity_type=kind, workspace=workspace)

	if referring:
		parts.append("")
		parts.append(f"Referred to by ({len(referring)})")
		parts.extend(
			f"#{one.ref}  {one.title}" + ("  (in a comment)" if one.via else "")
			for one in referring
		)

	# **What the writing suggests governs this, and nobody has confirmed** (`#1137`). Offered
	# rather than answered: a citation is written the same way whether it means *this follows
	# that decision* or *this contradicts it*, so this says what the evidence is and leaves the
	# judgement to whoever is reading. Confirming one is `subroutine_link`.
	#
	# **Below the links rather than among them**, because the whole value of the answer to
	# *what governs this* is that everything in it was agreed to by somebody.
	proposed = client.proposed_links(ref=ref, entity_type=kind, workspace=workspace)

	if proposed:
		parts.append("")
		parts.append(f"Not linked, but its writing suggests ({len(proposed)})")
		parts.extend(
			f"{one.label}  #{one.other.ref}  {one.other.title}  ({one.because})"
			for one in proposed
		)
		parts.append(
			f"Confirm one with subroutine_link(ref={proposed[0].other.ref}, "
			f"type='{proposed[0].link_type}', other={ref})"
		)

	if arguments.get("history"):
		parts.append("")
		parts.extend(
			f"{subroutine.views.moment_day(event.created_at, reading)}  {_happened(event)}"
			for event in client.history(ref=ref, entity_type=kind, workspace=workspace)
		)

	remarks = client.comments(ref=ref, entity_type=kind, workspace=workspace)

	if remarks:
		parts.append("")
		# **The date and the name, and the second half arrived with `#636`.** This used to
		# carry the date alone, on the argument that the alternative was the author's UUID —
		# thirty-six characters a model cannot resolve without another call, on every comment,
		# in the module whose whole argument is that context is a fixed cost. That argument
		# expired the day the response gained a username: a name is short, and on an instance
		# where five accounts in eight are agents *who wrote this* is the difference between a
		# colleague's note and a machine's.
		parts.extend(
			f"{subroutine.views.moment_day(remark.created_at, reading)}  "
			+ (f"@{remark.author}  " if remark.author else "")
			+ remark.body
			for remark in remarks
		)

	return _within_budget(
		parts,
		body_at=body_at,
		ref=ref,
		kind=kind,
		resume=max(0, int(arguments.get("from") or 0)),
	)


def _within_budget (
	parts: list[str], *, body_at: int | None, ref: int, kind: str, resume: int = 0
) -> str:
	"""Return this item's answer, trimming its body if the whole is more than it is worth.

	**Uncapped until now, where every other answer on this surface has been.** A 200 KB
	document came back whole — around fifty thousand tokens, in one call, from a tool an agent
	reaches for to *check* something. ``subroutine_call_api`` refuses the same object at 64 KB
	and names three ways to ask more narrowly.

	**Trimmed rather than refused, and that is the difference between the two.** ``call_api``
	refuses because its answer is JSON and a truncated JSON document is unparseable while
	still looking like a result — the caller cannot tell. This answer is prose written for a
	reader, so a cut that says it is a cut is legible, and refusing outright would be refusing
	to answer the question the tool exists for.

	**The body is what gives way**, not the end of the answer, because the links, the record
	and the tags are what a caller most often came for and they are written last. Where there
	is no body to trim the answer is left whole: everything else here is bounded by how many
	links and comments somebody wrote, and cutting those without saying which is worse than
	being long.

	``resume`` is where in the body to start, which is `#849`: **a cap is only defensible
	together with a way to read the rest.** Until now the cut note offered *all of it* — a
	terminal or the raw route — and never *the next part of it*, so for a 129 KB document the
	two available answers were 64 KB and 129 KB, and the remedy an agent was handed was the
	request that was already too big.

	**Characters, and the number is the one the note prints**, so continuing is copying a
	figure rather than computing one. Nothing else is repeated on a continuation: the links,
	the record and the tags came with the first page and sending them again would spend the
	budget on what the caller already has.
	"""

	body = None if body_at is None else parts[body_at]

	if resume > 0 and (body is None or resume >= len(body)):
		# **An offset past the end is answered, not ignored** (`#1177`). A header saying
		# *continuing at character N* above nothing at all is well-formed and unreadable: an
		# agent that copied the offset from a note, then called again after the body was
		# shortened, cannot tell that from *the rest was empty*. Saying how long the body
		# actually is turns a puzzle into an arithmetic mistake the caller can see.
		length = 0 if body is None else len(body)

		if length == 0:
			return f"#{ref}  has no description, so there is nothing to continue from."

		return (
			f"#{ref}  ends at character {length}, and you asked to continue at {resume}. "
			f"Nothing follows. Ask again with from={length} or less, or read the whole item "
			f"with subroutine_show(ref={ref})."
		)

	if body is not None and resume > 0:
		# **The body alone from here.** A continuation is the rest of one field, not a second
		# rendering of the item — everything around it was answered by the first call, and
		# repeating it is exactly the cost this whole mechanism exists to bound.
		parts = [f"#{ref}  continuing at character {resume}", "", body[resume:]]
		body_at = 2

	answer = "\n".join(parts)

	if len(answer) <= MAX_ANSWER or body_at is None:
		return answer

	body = parts[body_at]
	where = "/v1/tasks" if kind == "task" else "/v1/documents"
	# **Where to carry on from, computed after the allowance is known.** Written into the note
	# so the caller reads the number rather than working out what the cut cost — and it counts
	# from the start of the body, not from this page, so a third call is the same arithmetic
	# as the second.
	marker = "\n\n[… cut here at character {}. Continue with subroutine_show(ref={}, from={}). "
	tail = (
		f"The whole item is at 'subroutine show {ref}' in a terminal, or GET {where}/{ref} — "
		f"neither is capped.]"
	)
	# **Reserved at the widest the note can be, not at the narrowest.** The number it prints
	# depends on the allowance and the allowance depends on the note's length, which is
	# circular — so the room is booked for an offset no larger one can exist, and the few
	# characters that leaves unused are cheaper than an answer eight bytes over its own cap.
	widest = len(marker.format(resume + len(body), ref, resume + len(body))) + len(tail)
	allowance = max(0, len(body) - (len(answer) - MAX_ANSWER) - widest)
	stopped = resume + allowance
	parts[body_at] = body[:allowance] + marker.format(stopped, ref, stopped) + tail

	return "\n".join(parts)


class _Checkout(typing.NamedTuple):
	"""Where the checkout this session is standing in says work belongs, and what to say."""

	#: The project to file under, resolved against *this* instance. ``None`` means file
	#: wherever the caller's own arguments say, which is the workspace's Inbox by default.
	project: str | None

	#: The line to add to the answer, or ``None`` when there is nothing to report. Said even
	#: when the marker was *not* used, because an agent holding a repository whose file says
	#: one thing and an instance that says another needs to know which won.
	said: str | None


def _checkout (
	client: subroutine.clients.base.Client, *, workspace: str | None, overridden: bool
) -> _Checkout:
	"""Return the project a ``.subroutine`` marker here files into, and the line that says so.

	**One copy, because two handlers need this and only one of them had it** (`#1219`).
	``subroutine_add`` read the marker from the day it was written and ``subroutine_document``
	never did, so a document written from a marked checkout went to the workspace Inbox and the
	answer did not say where it had gone. Five accumulated before anybody noticed, which is what
	a silent write looks like from the outside.

	``overridden`` is the caller naming a project themselves — a ``+key`` in a captured line, or
	a ``project`` argument. Somebody speaking now outranks a file on disk (§13.7a).

	**Looked for on every call rather than at startup** (§13.7a, `#159`). A stdio server outlives
	the moment it was launched, and a repository adopted mid-session should not need it
	restarted — which is the one thing an agent cannot do to itself.
	"""

	marker = subroutine.directory.find()

	consulted = (
		marker is not None
		# **And only where the marker speaks for the connection this session is on** (`#414`).
		# A marker names one instance; its project is a fact about that instance and nothing
		# else. Without this, `directory.resolve`'s match-by-key fallback — which exists for
		# markers written before `#177` gave them ids — filed work into a same-named project on
		# whichever instance happened to answer.
		and marker.speaks_for(client.connection.name)
		and (marker.project is not None or marker.project_id is not None)
		and not overridden
	)

	if not consulted or marker is None:
		return _Checkout(None, None)

	# **Resolved against this instance, never passed through** (`#232`). The marker's key went
	# straight to the server until 0.1.0, so a checkout marked for somebody else's instance —
	# which is what committing this file is *for* — refused every write with "there is no
	# project 'SR' here", while the CLI beside it filed the task and said it had ignored the
	# marker. `#166` settled that the marker is advisory; only one surface implemented it.
	# Resolving also buys `#177`: a renamed project is followed by id, which this never did.
	filed = subroutine.directory.resolve(marker, client.projects(workspace=workspace))

	if filed is not None:
		return _Checkout(filed, f"in {filed}, from {subroutine.directory.FILE_NAME}")

	shown = marker.project or marker.project_id

	return _Checkout(
		None,
		f"{subroutine.directory.FILE_NAME} here names {shown!r}, which is not on this "
		f"instance. Ignoring it.",
	)


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

	line = _text(arguments, "text") or ""
	workspace = _text(arguments, "workspace")

	# **A `+key` in the line is somebody speaking now, and outranks a file on disk** (§13.7a).
	checkout = _checkout(
		client,
		workspace=workspace,
		overridden=subroutine.domain.capture.names_a_project(line),
	)

	captured = client.capture(
		text=line,
		workspace=workspace,
		type=_text(arguments, "type"),
		project=checkout.project,
		# **The second call an agent was measured skipping** (`#424`). `#392` put this on
		# `subroutine_update`, which made a described item two calls on two tools — and the
		# agent that reported this one said plainly why that loses: "an agent weighing calls
		# will systematically skip an optional second write, and the moment you have the most
		# context about an item is when you file it".
		description=_text(arguments, "description"),
		# **Absent checked first, because `_ref` raises rather than answering `None`** — it is
		# written for an argument that has to be there, and asking it about one that need not
		# be would refuse every capture that did not name a parent.
		parent=None if arguments.get("parent") is None else _ref(arguments, field="parent"),
	)
	answer = "Added " + _line(captured.task, now=subroutine.db.types.utcnow())

	# **A parent is an argument rather than a token, which is how it slipped past the rule
	# written for the grammar** (`#1191`). This function's whole argument is that a caller who
	# cannot see the parse cannot tell a deadline that was read from one that stayed in the
	# title — and a caller equally cannot tell a parent that was accepted from one that was
	# not. It took; nothing said so, on the one surface whose `(read …)` line is otherwise
	# scrupulous.
	if captured.task.parent_ref is not None:
		answer += f"\n  part of #{captured.task.parent_ref}"

	# Said out loud for the same reason the CLI says it: nobody typed it, and an agent that
	# cannot see where its work went cannot tell a person either. That argument applies just
	# as much when the marker was *not* used — more so, because the agent is then holding a
	# repository whose file says one thing and an instance that says another.
	if checkout.said is not None:
		answer = f"{answer}\n  {checkout.said}"

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
	"""Write a document, or revise the one a ref names.

	**The tool this adapter told agents to use and did not have** (`#138`). Until 2026-07-31
	``subroutine_comment``'s own description said "for a conclusion the next session needs,
	write a document instead", and there was no way to — a sentence in the agent-facing surface
	pointing at something that surface could not do.

	**And then it said the same thing about revising one** (`#822`). Its description ended
	"revise one with 'subroutine doc edit 42'" — a shell command, on the surface whose premise
	is having no shell. That half was corrected to name ``subroutine_call_api``, which was
	true and still asked an agent to leave the catalogue, read a schema and compose a PATCH in
	order to correct a sentence it had just written. What actually happens instead is a second
	document, which is the duplication `#47` exists to prevent.

	**One tool rather than two**, on ``subroutine_claim``'s precedent: writing a conclusion and
	correcting it are one capability in two directions, and an agent that has found the first
	has found the second in a description it reads whole. A separate tool would spend a name
	and a schema in every session on a verb reached only after this one.

	``title`` is no longer required by the schema, because a revision that only changes the
	body should not have to restate the title — restating it is how a document is silently
	renamed by a model reconstructing it from memory. So the pair is refused here instead,
	naming both arguments this tool actually has (`#547`).
	"""

	ref = arguments.get("ref")
	workspace = _text(arguments, "workspace")

	if ref is None:
		if not _text(arguments, "title"):
			raise ValueError("Pass title to write a document, or ref to revise one.")

		# **The checkout decides where a conclusion is filed, exactly as it decides where a task
		# is** (`#1219`). This read no marker at all until 2026-08-24, so a document written from
		# a marked repository landed in the workspace Inbox — and the answer below named the ref
		# and not the project, so nothing on this surface could tell an agent it had happened.
		# Five documents accumulated that way, two of them ones a session is told to read
		# before starting work.
		#
		# **A `project` argument is the caller speaking now and still wins**, which is the same
		# precedence a `+key` in a captured line has.
		checkout = _checkout(
			client,
			workspace=workspace,
			overridden=_text(arguments, "project") is not None,
		)

		document = client.create_document(
			title=_text(arguments, "title") or "",
			body=_text(arguments, "body"),
			type=_text(arguments, "type"),
			project=_text(arguments, "project") or checkout.project,
			tags=_words(arguments, "tags"),
			workspace=workspace,
		)

		answer = "Wrote " + _line(document, now=subroutine.db.types.utcnow())

		return answer if checkout.said is None else f"{answer}\n  {checkout.said}"

	# **Omitted is unchanged, and that is the whole reason this is worth a ref** (§8.3). An
	# agent correcting one paragraph sends the body; the type, the project and the tags it
	# decided on when it wrote the thing stay as they were.
	#
	# ``UNSET`` rather than ``None`` for each, because on this signature ``None`` *clears* the
	# field. A comprehension splatting only what was given reads more neatly and is untypeable
	# — mypy sees one value type for the whole mapping, which is exactly the looseness §6.3a's
	# ``typing.Any`` lesson says is where the next defect hides.
	def said (name: str) -> typing.Any:
		"""Return one argument, or the sentinel meaning the caller did not mention it."""

		value = _text(arguments, name)

		return subroutine.clients.base.UNSET if value is None else value

	tags = _words(arguments, "tags")

	revised = client.update_document(
		ref=_ref(arguments),
		title=said("title"),
		body=said("body"),
		type=said("type"),
		project=said("project"),
		tags=subroutine.clients.base.UNSET if tags is None else tags,
		workspace=workspace,
	)

	# **A revision consults no marker, deliberately.** Omitted means unchanged (§8.3), so a
	# document keeps the project it was filed under; letting the checkout speak here would move
	# somebody else's document because of where the editor happened to be standing.
	return "Revised " + _line(revised, now=subroutine.db.types.utcnow())


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
	"""Finish a task, and say what the claim on it is still doing — `#777`.

	**`#705` mandated claiming on 2026-08-09 and nothing changed.** Measured the next day: nine
	items opened and closed, none claimed, none passed through `in_progress`, and the event
	history empty of both. Measured again by `#777`: still true. The instruction shipped and
	the behaviour did not, for the one agent working here, which is the whole population.

	It went into the skill, and the skill reaches a session through a **plugin cache that
	lags** — installed 0.6.1 against a manifest at 0.6.4, and 0.6.1's copy did not carry the
	sequence at all. So the item's third condition is that whatever notices must survive that.
	**This does, structurally rather than by luck**: since `#539` these tools run on the
	*instance*, so a caller's cached plugin cannot be a version of this behind.

	Two clauses, and only ever one of them:

	- **Still claimed.** ``done`` does not release, measured rather than assumed — so an agent
	  following half the advice leaves a trail of claims on finished work. The one clause here
	  that is actionable at the moment it is read.
	- **Never claimed.** Advice about the next item rather than this one, which is the honest
	  framing: a claim cannot be taken retroactively. It is here because the moment of closing
	  is when the sequence is most legible, and because the guaranteed channel is the program.

	**On this surface and not at the command line.** §1.4: a person finishing *buy milk* has
	not asked about claims and must not be told about them. `#705`'s rule is for agents, and
	this is the agents' surface.
	"""

	ref = _ref(arguments)
	workspace = _text(arguments, "workspace")

	# **Asked before finishing, because finishing releases** (`#1113`). The task that comes
	# back carries no claim whether it had one or not, so the answer below could no longer tell
	# *you did not claim this* — a correction aimed at an agent that skipped a step — from
	# *and the claim went back*. One read on a tool that already writes is the cheaper half of
	# that trade; guessing would put the wrong sentence in front of somebody doing it right.
	# `client.task` answers `None` for a ref that is not there; the write below refuses it by
	# name a moment later, which is the message worth showing, so this simply has nothing to
	# say about a task that does not exist.
	before = client.task(ref=ref, workspace=workspace)
	held = None if before is None else before.claimed_by

	# **Two verbs, one tool.** Both end this occurrence and both bring the next; what differs
	# is which fact is recorded about the month, and a series recorded entirely as done cannot
	# answer *how often do I actually skip this* (`#574`).
	if arguments.get("skip"):
		skipped = client.skip(ref=ref, workspace=workspace)
		said = f"Skipped: {skipped.title}."
		finished = skipped

	else:
		finished = client.complete(ref=ref, workspace=workspace)
		said = f"Done: {finished.title}."

	# **The reminder to hand it back is gone, because finishing now does** (`#1113`). This
	# said *still claimed by @you — release it*, which was true, actionable and asked for the
	# one thing that reliably does not happen: an obligation falling at the end of a session
	# is one nobody attends, because the end of a session is compaction or a killed process.
	#
	# What is left is the half that is about the *next* item, which a reader can still act on.
	if held:
		return f"{said} The claim on it went back with it."

	return (
		f"{said} It was not claimed — claim one before you start it, so "
		f"nobody else takes the same work."
	)


def _ref (arguments: dict[str, typing.Any], *, field: str = "ref") -> int:
	"""Return the ref an argument names, accepting ``42`` and ``"#42"`` alike.

	A model reads ``#42`` everywhere this system writes an address, so it will send that back
	sooner or later — and refusing it over a sigil would be refusing the caller its own
	notation (§6.2).

	**``field`` exists because ``subroutine_link.other`` was the one ref this did not read.**
	It published ``A_REF`` — both spellings, because both work — and then checked
	``isinstance(other, int)`` by hand, so the schema promised a string and the tool refused
	one. The refusal said *pass the number in the listing*, and every listing writes that
	number ``#2``: the value that had just failed, offered back as the remedy. `A_REF`'s own
	comment describes the inverse of this as the thing it was added to stop.
	"""

	given = arguments.get(field)

	if isinstance(given, bool) or given is None:
		raise ValueError(f"Which item? Pass {field!r}, the number in the listing.")

	found = subroutine.domain.refs.parse_ref(str(given))

	if found is None:
		# `parse_ref` returns None for anything that cannot *be* a ref — a zero, a leading
		# zero, a number too large for the column. Refused here with the value in it, rather
		# than passed on as a lookup that would come back "there is no such item" about
		# something that was never an item.
		raise ValueError(f"{given!r} is not an item number.")

	return found


def _account_zone (client: subroutine.clients.base.Client, workspace: str | None) -> str:
	"""Return the account's zone here — §6.5 resolved by the instance, not by this process.

	**One function because decision `#1088` asks one question twice.** A day an agent *writes*
	is read in the setter's zone and a moment it *reads* is rendered in the reader's; an agent
	is one account, so both are this. It was called ``_typed_day_zone`` while writing was its
	only caller, which made the name a claim about the use rather than about the value
	(`#1091`).

	Resolved by the instance and published on ``/v1/meta``, which
	``identity()`` is already the answer to. Nothing extra is fetched on the path that matters:
	since `#539` these tools run inside the instance for a relayed connection, so this is a
	local call rather than a round trip.

	**What it replaces was nobody's zone.** ``config.system_timezone()`` is the *process's* —
	the agent's machine when the connection is local, and the server's ``/etc/localtime`` for
	every relayed one. So the day an agent named was decided by whichever host happened to be
	running the adapter, and `#1083` had the same shape one file along at the terminal.

	Falls back to the process's zone only where the instance sends no such key, which is one a
	release behind (`#345`) — the answer it gave before, rather than a refusal for a field that
	has only just started existing.
	"""

	# **``me`` rather than ``identity``**, and the reason is a guard rather than taste:
	# ``identity`` is excused from this surface on §21.2's budget, and `test_reach` refuses a
	# method that is both excused and called here — correctly, because an excuse claiming a
	# capability is absent and a call reaching it are two statements that must not disagree.
	# ``subroutine_whoami`` already reaches ``me``, so nothing about what an agent can do moves.
	reachable = client.me().workspaces
	wanted = None if workspace is None else workspace.strip().lower()

	for candidate in reachable:
		if wanted is not None and candidate.slug != wanted:
			continue

		if candidate.reader_timezone is not None:
			return candidate.reader_timezone

		break

	return subroutine.config.system_timezone()


def _day (given: typing.Any, *, field: str, timezone: str) -> datetime.date | None:
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

	moment = _moment(given, field=field, timezone=timezone)

	if isinstance(moment, datetime.datetime):
		# Back to a day in the zone the moment was read in — the same conversion
		# `schedule.interpret_written_day` does, and for its reason: reading it in UTC would
		# make a Friday evening into Saturday for anybody east of Greenwich.
		return subroutine.domain.schedule.day_in(moment, timezone)

	return moment


def _moment (
	given: typing.Any, *, field: str, timezone: str
) -> datetime.datetime | datetime.date | None:
	"""Read a day an agent named, **keeping a time of day when it wrote one** (`#858`).

	`_day`'s sibling and its implementation — `_day` is this with the clock thrown away, so
	the two vocabularies cannot drift apart. Which fields take which is decided at the one
	call site, where the reason can be read beside both.

	A weekday, a bare date and a §9.3 expression all name a day; only a written time is
	honoured, which is the rule ``schedule.interpret_written_moment`` states in full.

	**``timezone`` is the account's, passed in** (`#1064`, decision `#1088`). It used to be
	``config.system_timezone()`` read right here, which is the *server's* for every relayed
	connection — see :func:`_account_zone` for why that was nobody's zone.
	"""

	if not isinstance(given, str):
		raise ValueError(f"{field!r} is a day, written like 'friday' or '2026-09-01'.")

	if not given.strip():
		return None

	# **The refusal is the domain's, not one written here** — `interpret_written_moment` names
	# the whole typed vocabulary, weekdays first, so an agent and a person are told the same
	# thing in the same words. A second message here would be a place for the two to drift.
	return subroutine.domain.schedule.interpret_written_moment(
		given,
		timezone=timezone,
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
	other = _ref(arguments, field="other")

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

		# **Both ends, because a reversed call answers just as plausibly as a correct one**
		# (`#1190`). Direction is the most confusable thing here — the skill spends a paragraph
		# on *`ref` is the blocker* — and a one-ended echo cannot disconfirm the mistake it is
		# most likely to be read after: it names the item you did not mean to gate, in exactly
		# the words a correct call would use. The withdraw branch below already named both.
		#
		# `made.label` is the relation as seen from `ref`, so an inverse link reads
		# `#4 Blocked by #3` and stays true rather than needing the forward name.
		return f"#{ref} {made.label} #{made.other.ref}  {made.other.title}"

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


def _words (arguments: dict[str, typing.Any], name: str) -> list[str] | None:
	"""Return one array-of-strings argument, refusing anything the schema does not allow.

	**Both halves are this function's, and that is unlike every other argument here.**
	``protocol._mistyped`` refuses a value whose type does not match the schema — but its
	``_ACCEPTS`` deliberately knows nothing about ``array``, and its own comment says why: a
	schema growing one should be a rule somebody adds rather than something that quietly starts
	being rejected. So a bare string reaches here, and so does a list carrying a number.

	Returning ``None`` for either would be `#379` exactly — an argument swallowed, with the
	caller told nothing and the write proceeding as though they had asked for it.

	``None`` *is* right for an absent argument and for an empty list, because both mean "no
	tags" on a create and there is nothing to clear on something that does not exist yet.
	"""

	given = arguments.get(name)

	if given is None:
		return None

	if not isinstance(given, list) or not all(isinstance(word, str) for word in given):
		raise subroutine.errors.ValidationError(
			f"{name!r} takes a list of words.",
			errors=[
				subroutine.errors.FieldError(
					field=name,
					code="invalid_field_value",
					message=f"{name!r} was {type(given).__name__}, not a list of strings.",
					hint='Write it as ["design", "security"].',
				)
			],
		)

	return given or None


def _updated (
	client: subroutine.clients.base.Client, arguments: dict[str, typing.Any]
) -> str:
	"""Change a task's own fields, and report what it looks like now.

	**Only the fields an agent actually re-decides**, not everything ``PATCH /v1/tasks``
	accepts. Every property here is schema carried by every session of every agent, including
	the ones that never call this, so the dates stay off it — those are ``schedule``'s.

	**The assignee was off it too, and `#493` re-weighed that under §21.2.** The reason given
	was that an assignee is *a person's* concern, which was decided when no surface could
	reassign at all — so it described a gap rather than a boundary. The test §21.2 actually
	sets is *what would an agent get wrong without it*, and the answer is the whole of the
	hand-back move: an agent that cannot finish something has no way to give it back to
	whoever asked, which is the loop `#507` is designed around. Measured at 99 bytes against
	418 spare, so it needed no cap raise — and the dates, tags and timezone stay off because
	nothing an agent does goes wrong for want of them.

	Nothing given is a refusal rather than a no-op: an agent that meant to change something
	and named no field has made a mistake, and a cheerful "unchanged" would hide it.
	"""

	changes: dict[str, typing.Any] = {}

	# `''` clears it, matching `plan` and `defer` below rather than inventing a third way to
	# say "no longer set" on one surface.
	if "assignee" in arguments:
		changes["assignee"] = arguments["assignee"] or None

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

	# **`''` stops the series rather than clearing a column** (`#94`), which is the same
	# reading `assignee`, `plan` and `defer` above give an empty string — so this surface does
	# not invent a fourth way to say *no longer set*. What it stops is the whole repeat: the
	# occurrence in hand keeps its number and its record, and nothing follows it.
	if "repeat" in arguments:
		changes["recurrence"] = arguments["repeat"] or None

	ref = _ref(arguments)
	workspace = _text(arguments, "workspace")

	# **``defer`` reads a clock and ``plan`` does not** (`#858`). ``snoozed_until`` carries a
	# time everywhere it is stored, so a surface that truncates is throwing away something the
	# writer said; ``starts_at`` is rendered by nothing at this scale yet, which is `#576`.
	#
	# Both are here rather than in one branch because the alternative is worse than the bug:
	# `#858` fixed ``subroutine defer`` at the terminal, and stopping there would have left
	# one field meaning two different things depending on which surface set it — the
	# divergence this codebase spends most of its time removing, introduced by the fix for
	# something else.
	#
	# **The zone is looked up once, and only when a day was actually named** (`#1064`). It was
	# read from the process inside each helper before, which is the server's for every relayed
	# connection — see :func:`_account_zone`. Asking here rather than in the helpers is what
	# keeps a change with no dates in it from fetching an identity it has no use for.
	days: dict[str, datetime.datetime | datetime.date | None] = {}

	if any(field in arguments for field in ("plan", "defer")):
		zone = _account_zone(client, workspace)

		days = {
			field: (_moment if field == "defer" else _day)(
				arguments[field], field=field, timezone=zone
			)
			for field in ("plan", "defer")
			if field in arguments
		}

	if not changes and not days:
		raise ValueError(
			"Nothing to change. Pass importance, urgency, estimate, status, type, title, "
			"description, repeat, plan or defer."
		)

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
				for field, name in (("plan", "starts"), ("defer", "snooze"))
				if field in days
			},
		)

	if changed is None:
		raise LookupError(
			f"There is no #{ref} here. Run subroutine_list to see what there is, or "
			f"subroutine_search to look for it by words in its title."
		)

	# **What moved, beside what it now is** (`#1186`'s sibling, `#1196`). The row alone was
	# the whole item *minus* the field just written — a defer to `now+3M` answered with an
	# unchanged-looking line and cost a `subroutine_show` to learn it had landed in November.
	#
	# **A relative date is the case that earns this.** `now+3M` and `friday` cannot be checked
	# by inspection, so the resolved day is the only confirmation there is; the scalars are
	# carried too because a caller that sent four fields should not have to diff a row to see
	# that all four took. This is `subroutine_add`'s `(read …)` parenthetical, which is the
	# thing on this surface agents report relying on, applied to the write next door.
	settled = [f"{name} {value}" for name, value in sorted(changes.items()) if value is not None]
	settled += [f"{name} cleared" for name, value in sorted(changes.items()) if value is None]
	# **`plan` is a day and `defer` is a moment**, which is `#858`'s distinction and the reason
	# these cannot share one renderer: a day is a label that never converts, and a moment has no
	# day until somebody names a zone — which :func:`_day_of` reads off the item.
	for field in ("plan", "defer"):
		if field not in days:
			continue

		when = days[field]

		if when is None:
			settled.append(f"{field} cleared")
		elif isinstance(when, datetime.datetime):
			settled.append(f"{field} {_day_of(when, changed)}")
		else:
			settled.append(f"{field} {when.isoformat()}")

	said = f"  (set {', '.join(settled)})" if settled else ""

	return "Changed " + _line(changed, now=subroutine.db.types.utcnow()) + said
