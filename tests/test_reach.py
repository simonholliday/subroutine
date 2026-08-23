"""What one surface can do and another cannot — docs/design.md §13.7, items ``#139`` and ``#148``.

**Three walls in one afternoon, all the same shape, all found by hand.** ``#134``: a project
could not be created outside HTTP, so on a default install — where nothing runs ``serve`` — the
only project anybody would ever have was the Inbox. ``#136``: readiness, the one question this
tool answers that a list of tasks does not, was a query parameter and nothing else. ``#138``: a
document could not be written, while the MCP adapter's own tool description told agents to
write one.

Each was found by trying to *write documentation* and discovering the sentence could not be
true. That is a slow and lucky way to find a structural gap, and there was no reason to think
three was all of them.

**The rule, agreed with Simon on 2026-08-01 (``#146``): a capability reaches all three surfaces
unless somebody wrote down why not.** Not "everything everywhere" — three recorded constraints
would each break under that, and they are named in :data:`KINDS`. The value is ``#139``'s
argument unchanged: skipping one becomes a decision somebody recorded rather than an omission
nobody noticed.

**This file guards three edges, and the first version guarded one.** That is why ``#145`` — an
agent could not comment on a document — was found by hand five minutes after ``#99``, with the
guard passing the whole time:

1. **Mutating routes against the client protocol.** The original, and the one that found eleven.
2. **Reading routes against the client protocol.** Added by ``#148``. ``GET
   /v1/tasks/{ref}/events`` reached no client at all, so an item's history was unreadable from
   either surface — including on the morning ``#52`` shipped to put comments into it.
3. **The protocol against the CLI and against MCP**, derived by reading the source rather than
   declared, so a cell cannot claim reach it does not have.

**What it does not check, stated because a guard that overstates itself is worse than none.**
Reach here is *per method*. A capability that is an argument on a method both surfaces already
call is invisible to it — which is exactly how MCP came to have no search (``client.tasks``
takes ``q``; the tool does not) and no defer (``client.schedule`` takes ``start``). Those are
``#149``. Deriving argument-level reach is not possible where a call site passes ``**changes``,
and declaring it would be a third copy of the matrix in ``#146``.
"""

import ast
import inspect
import pathlib
import re
import typing

import subroutine.api.app
import subroutine.api.problems
import subroutine.api.routing
import subroutine.cli.main
import subroutine.clients.base
import subroutine.clients.http
import subroutine.mcp.tools

#: The repository root, resolved from this file rather than from the working directory.
#: `conftest`'s `_no_inherited_directory` moves every test somewhere with no `.subroutine`
#: above it (§13.7a), which is right — and it turned three checks here from "reads the source"
#: into "reads nothing and passes", because they were relative to wherever pytest was started.
ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The verbs that change something. A reader that no client can reach is a missing feature; a
#: *writer* that no client can reach is a capability the product has and does not offer.
MUTATING = frozenset({"POST", "PATCH", "PUT", "DELETE"})

#: And the one that reads. Separate lists, because the two fail differently: a missing writer
#: is work nobody can do, a missing reader is an answer nobody can get.
READING = frozenset({"GET"})

#: Why a capability legitimately stops at one surface. **Each names a constraint rather than a
#: preference**, which is the whole difference between an exemption and a shrug.
#:
#: - ``budget`` — the MCP schema is context every agent carries whether or not it calls the
#:   tool. Held by ``tests/test_mcp.py``, which is where the current figure is; it is not
#:   repeated here, because the copy that was ("7 tools / 4,608 bytes") was two tools and a
#:   third of the size out of date by the time `#198` found the same number stale in the
#:   README, the CHANGELOG and one other comment. An entry weighs the addition against what
#:   the tool buys.
#: - ``disclosure`` — §1.4. The capability exists and is deliberately not on a beginner's
#:   first screen, or is deliberately not one line.
#: - ``administrative`` — §12.4's recovery property: it must work when the service will not
#:   start, which means the CLI owns it and a client method would be a worse second path.
#: - ``protocol`` — it belongs to the transport rather than to the product. A health check is
#:   not a capability somebody is being denied.
#: - ``tracked`` — not built, and an item says so. **Must name a ref**, so that "later" cannot
#:   be written by somebody in a hurry and read as considered.
KINDS = ("budget", "disclosure", "administrative", "protocol", "tracked")

#: One exemption: which kind, and the prose that argues it.
Excuse = tuple[str, str]

#: Mutating routes, and the :class:`~subroutine.clients.base.Client` method that reaches each.
REACHED_BY: dict[tuple[str, str], str] = {
	("POST", "/v1/tasks"): "capture",
	# Curating the vocabulary — `SR#826`.
	("POST", "/v1/statuses"): "create_status",
	("PATCH", "/v1/statuses/{which}"): "update_status",
	("DELETE", "/v1/statuses/{which}"): "delete_status",
	("POST", "/v1/link-types"): "create_link_type",
	("PATCH", "/v1/link-types/{which}"): "update_link_type",
	("DELETE", "/v1/link-types/{which}"): "delete_link_type",
	("POST", "/v1/tags"): "create_tag",
	("PATCH", "/v1/tags/{which}"): "update_tag",
	("DELETE", "/v1/tags/{which}"): "delete_tag",
	("PATCH", "/v1/tasks/{id_or_ref}"): "update",
	("POST", "/v1/tasks/{id_or_ref}/complete"): "complete",
	("POST", "/v1/tasks/{id_or_ref}/skip"): "skip",
	("POST", "/v1/recurrence/parse"): "read_repeat",
	("POST", "/v1/tasks/{id_or_ref}/comments"): "remark",
	("POST", "/v1/documents"): "create_document",
	("POST", "/v1/users"): "create_user",
	("PATCH", "/v1/users/{username}"): "set_active",
	("POST", "/v1/workspaces/{id_or_slug}/members"): "add_member",
	("DELETE", "/v1/workspaces/{id_or_slug}/members/{username}"): "remove_member",
	("POST", "/v1/documents/{id_or_ref}/comments"): "remark",
	("DELETE", "/v1/comments/{comment_id}"): "uncomment",
	("POST", "/v1/workspaces"): "create_workspace",
	("PATCH", "/v1/workspaces/{id_or_slug}"): "rename_workspace",
	("DELETE", "/v1/workspaces/{id_or_slug}"): "delete_workspace",
	("POST", "/v1/workspaces/{id_or_slug}/restore"): "restore_workspace",
	("PATCH", "/v1/documents/{id_or_ref}"): "update_document",
	("POST", "/v1/projects"): "create_project",
	("POST", "/v1/tokens"): "issue_token",
	("POST", "/v1/calendars"): "create_calendar",
	("POST", "/v1/calendars/{id_or_prefix}/reset"): "reset_calendar",
	("DELETE", "/v1/calendars/{id_or_prefix}"): "revoke_calendar",
	("POST", "/v1/login-links"): "create_login_link",
	("POST", "/v1/users/{username}/signout"): "sign_out_everywhere",
	("DELETE", "/v1/tokens/{id_or_prefix}"): "revoke_token",
	("PATCH", "/v1/projects/{id_or_key:path}"): "rename_project",
	("POST", "/v1/projects/{id_or_key:path}/move"): "move_project",
	("POST", "/v1/projects/{id_or_key:path}/comments"): "remark",
	("DELETE", "/v1/tasks/{id_or_ref}"): "discard",
	("DELETE", "/v1/documents/{id_or_ref}"): "discard",
	("POST", "/v1/tasks/{id_or_ref}/claim"): "claim",
	("POST", "/v1/tasks/{id_or_ref}/release"): "release",
	("POST", "/v1/tasks/{id_or_ref}/restore"): "undiscard",
	("POST", "/v1/documents/{id_or_ref}/restore"): "undiscard",
	("POST", "/v1/tasks/{id_or_ref}/move"): "move",
	("POST", "/v1/documents/{id_or_ref}/move"): "move",
	("POST", "/v1/tasks/{id_or_ref}/links"): "link",
	("DELETE", "/v1/tasks/{id_or_ref}/links/{link_id}"): "unlink",
	("POST", "/v1/documents/{id_or_ref}/links"): "link",
	("DELETE", "/v1/documents/{id_or_ref}/links/{link_id}"): "unlink",
}

#: Reading routes, and the method that reaches each.
READ_BY: dict[tuple[str, str], str] = {
	# Curating the vocabulary — `SR#826`.
	("GET", "/v1/statuses"): "statuses",
	("GET", "/v1/link-types"): "link_types",
	("GET", "/v1/tags"): "tags",
	("GET", "/v1/agenda"): "agenda",
	("GET", "/v1/tasks"): "tasks",
	("GET", "/v1/tasks/{id_or_ref}"): "task",
	("GET", "/v1/tasks/{id_or_ref}/occurrences"): "occurrences",
	("GET", "/v1/tasks/{id_or_ref}/comments"): "comments",
	("GET", "/v1/tasks/{id_or_ref}/links"): "links",
	("GET", "/v1/tasks/{id_or_ref}/backlinks"): "backlinks",
	("GET", "/v1/documents"): "documents",
	("GET", "/v1/documents/{id_or_ref}"): "document",
	("GET", "/v1/documents/{id_or_ref}/comments"): "comments",
	("GET", "/v1/documents/{id_or_ref}/links"): "links",
	("GET", "/v1/documents/{id_or_ref}/backlinks"): "backlinks",
	("GET", "/v1/projects"): "projects",
	("GET", "/v1/projects/{id_or_key:path}/comments"): "comments",
	("GET", "/v1/tasks/{id_or_ref}/events"): "history",
	("GET", "/v1/changes"): "changes",
	("GET", "/v1/documents/{id_or_ref}/events"): "history",
	# **These two were swapped, and swapped consistently, so every check here passed** (`#336`).
	# `identity()` calls `/v1/meta` and says so in writing; `/v1/me` was mapped to it and
	# `/v1/meta` sat in NOT_REACHED. Nothing was unclassified and nothing was both reached and
	# excused, so the guard reported a capability that did not exist and hid the whole of "who
	# am I and what may I do". **The mapping itself is still unverified** — this file checks
	# that a method of that *name* exists, never that it calls the route it is written beside,
	# and it cannot, because the local client uses no paths at all. `#340` is that gap.
	("GET", "/v1/me"): "me",
	("GET", "/v1/meta"): "identity",
	# **Both were excused until `#483`**, on the grounds that "somebody holding a client has
	# already got past the problem it solves". That was written from the CLI, which has `--help`
	# and `explain`; an agent over MCP holds a client and has neither, so it could not read the
	# guide written for it. `reference()` is what an MCP resource fetches.
	("GET", "/v1/docs/agent"): "reference",
	("GET", "/v1/docs/examples"): "reference",
	("GET", "/v1/tokens"): "tokens",
	("GET", "/v1/calendars"): "calendars",
	("GET", "/v1/users"): "users",
	("GET", "/v1/workspaces/{id_or_slug}/members"): "members",
}

#: Routes no client reaches, and why. **Deleting an entry is what closes it.**
NOT_REACHED: dict[tuple[str, str], Excuse] = {
	("POST", "/v1/admin/backups"): (
		"administrative",
		"It needs `instance:admin`, which no role carries, and §12.4's recovery property "
		"wants `subroutine db backup` to work when the service will not start. A client "
		"method would be a second path to a thing the CLI already does better.",
	),
	("GET", "/v1/admin/backups"): (
		"administrative",
		"The catalogue beside the backup itself, and `subroutine db backups` reads it "
		"without a running service, which is the whole of §12.4.",
	),
	("GET", "/healthz"): (
		"protocol",
		"A liveness probe for whatever is in front of this. Not a capability anybody is "
		"being denied, and §10.4's `subroutine doctor` is the question a person asks.",
	),
	("GET", "/v1/calendars/{prefix}/{secret}.ics"): (
		"protocol",
		"An address a *calendar application* fetches, not a call a client makes (`#916`, "
		"§20.2). It answers `text/calendar` rather than a view model, it takes its whole "
		"credential from the path, and what a person does with it is paste it into Apple "
		"Calendar — so a client method returning parsed iCalendar would be a capability "
		"nobody has asked for and a second reason for this route to exist. What a client "
		"does need is the ability to *manage* feeds, and that is a separate surface.",
	),
	("GET", "/"): (
		"protocol",
		"The browser app's page (`#597`). A `Client` method returning HTML would be a method "
		"for being a browser, and the thing it serves talks to this API over the same routes "
		"every client already reaches — so there is no capability here that is not already "
		"classified somewhere above.",
	),
	("GET", "/app/{name}"): (
		"protocol",
		"The browser app's own files (`#597`). Same reason as the page they belong to: these "
		"are bytes a browser needs and nothing a client would ever ask for.",
	),
	("GET", "/mcp"): (
		"protocol",
		"A refusal, not a capability (`#648`): this transport has no server-initiated messages, "
		"so the standalone event stream a client opens is answered `405 Allow: POST`. It was "
		"measured against `claude-code/2.1.222` and had always come from there being no route "
		"at all — it is declared now because an absence can be claimed by whatever matches "
		"next, and `#647`'s catch-all did exactly that.",
	),
	("GET", "/signin"): (
		"protocol",
		"A browser navigation that trades a link for a cookie (`#248`). A client method "
		"would have to hold a cookie jar to mean anything, and none of them does — the CLI "
		"and MCP both authenticate with a bearer token, which this route exists to avoid "
		"needing. `subroutine login link` is how a person is given one.",
	),
	("DELETE", "/v1/session"): (
		"protocol",
		"The other half of `/signin` (`#248`), and unreachable for the same reason: a caller "
		"with no cookie has no session to end, and one holding an API token is told so "
		"rather than answered. `subroutine login revoke` is the operator's equivalent.",
	),
	("POST", "/v1/session"): (
		"protocol",
		"Confirming a switch of accounts in a browser (`#803`). It is submitted by a form on "
		"a page this application served, in `application/x-www-form-urlencoded`, and it "
		"refuses any caller whose credential is not a cookie — so a client method would be a "
		"method nothing could call. It exists at all because `GET /signin` is public and "
		"therefore has no origin check; this one has, by requiring the standing session.",
	),
	("POST", "/mcp"): (
		"protocol",
		"The MCP transport itself (`#516`). It carries the tools a client already reaches "
		"rather than offering anything beside them, so a `Client` method here would be a "
		"method for speaking a protocol rather than for doing a thing — and it would reach "
		"the same services the client is already sitting on. `#539`'s proxy will POST to it, "
		"and a transport adapter is not a capability either.",
	),
	("GET", "/readyz"): (
		"protocol",
		"The same, plus the schema check §12.4a wants before a client trusts an instance. "
		"A client that could reach it would be asking whether the thing it is already "
		"talking to is up; `#89` is that question asked properly, by the CLI, before it "
		"connects at all.",
	),
	("GET", "/v1/projects/{id_or_key:path}"): (
		"tracked",
		"One project on its own. `project list` prints the tree and `show` reads items; "
		"nothing yet asks for a single project's own record. `#141`.",
	),
	("GET", "/v1/projects/{id_or_key:path}/events"): (
		"tracked",
		"A project's history. `#150` gave tasks and documents theirs, which is where `show` "
		"reaches; a project has no `show` of its own to grow a section on — `#141`.",
	),
	("GET", "/v1/workspaces"): (
		"tracked",
		"`init` makes one and most installations need exactly one, so listing them is a "
		"real gap without being a wall. `#141`.",
	),
	("GET", "/v1/workspaces/{id_or_slug}"): (
		"tracked",
		"Reading one workspace's own record. Same as listing them, and rarer. `#141`.",
	),
	("PATCH", "/v1/comments/{comment_id}"): (
		"tracked",
		"Only the author may edit a comment, and deleting is the honest alternative to "
		"rewriting attributed prose — which `#400` built, so this half stays HTTP-only "
		"deliberately rather than for want of asking. `#141` if somebody wants it.",
	),
	("DELETE", "/v1/projects/{id_or_key:path}"): (
		"disclosure",
		"Deleting a project takes its tasks out of the visible world with it. That wants "
		"confirmation and a considered message rather than the same one-liner as `#140`. "
		"`#141`.",
	),
	("POST", "/v1/projects/{id_or_key:path}/restore"): (
		"disclosure",
		"The other half of the entry above, and it goes with it. `#308` built the endpoint "
		"because `DELETE` promised a reversal nothing provided; a client verb for putting a "
		"project back, where taking it away is still HTTP-only, would be the pair reachable "
		"from opposite directions. `#141` closes both.",
	),
}

#: Client methods the CLI does not call, and why.
NOT_IN_CLI: dict[str, Excuse] = {
	"statuses": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"create_status": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"update_status": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"delete_status": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"link_types": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"create_link_type": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"update_link_type": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"delete_link_type": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"tags": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"create_tag": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"update_tag": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"delete_tag": (
		"protocol",
		"`SR#1129`. The capability is built on every layer beneath the CLI — a domain service, an API module, and both clients — and twelve commands under `subroutine workspace` are what is missing. Split out of `SR#826` rather than folded in, because that item is about three permissions that gated nothing and they gate something now; a terminal is delivery rather than enforcement, and one commit covering a service, an API, twelve client methods, an error code and a dozen commands is more than one gate run can honestly cover.\n\n**§1.4 is what decides where they go**: under `workspace`, never on the personal path, because somebody keeping a to-do list must not meet a status listing before setting a status. **Deleting these entries is what closes `SR#1129`.**",
	),
	"occurrences": (
		"disclosure",
		"`SR#94`, §6.7. This exists for a **calendar**, and a terminal is not one: `show` "
		"already reads the rule back as a sentence — *every month, on the 30th* — which is "
		"the fact a person acts on, where a list of instants is the thing a month grid is "
		"drawn from. A command printing the next five dates would be a second way to ask what "
		"`show` answers, which is the duplication `SR#154` closed for `help` and `explain`.\n\n"
		"**What would remove it**: a date-ranged view at the terminal. `subroutine agenda` is "
		"the nearest thing and deliberately spans one day; `SR#916`'s feed is where a range "
		"gets a consumer, and `SR#576` is where an event acquires a span to draw.",
	),
	"read_repeat": (
		"disclosure",
		"`#94`. §6.7 built this so a caller can confirm a phrase *before* committing to it, and at a terminal there is nothing to confirm before: `subroutine add \"Pay the rent on the 30th of every month\"` answers with the repeat rendered back from the stored rule — *every month, on the 30th* — in the same breath as creating it. A preview command would be a second way to ask a question the create already answers, which is the duplication `#154` closed for `help` and `explain`.\n\n"
		"**What would remove it**: a surface where the answer arrives too late to act on. That is a form, and the browser calls this endpoint directly.",
	),
	"call_api": (
		"protocol",
		"`#485`. This exists because the *tool* surface is a budget — decision `#484` measured "
		"thirteen of twenty missing capabilities as excluded for room rather than by any "
		"decision — and the CLI has no such budget. It is the complete product, so there is "
		"nothing for a person at a terminal to escape from: a raw-request command would be a "
		"second way to do what a command already does, which is the defect `#154` closed for "
		"`help` and `explain`.\n\n"
		"**What would close this is the CLI falling behind the API the way MCP did**, and the "
		"route-side checks in this file are what would say so first. Deleting this entry is "
		"what would record it.",
	),
	"reference": (
		"protocol",
		"`#483`. §13.3's guide is written *for an agent* — it opens with what a caller with a "
		"base URL and a token gets, and names what is unbuilt. A person at a terminal has a "
		"better answer to the same question already: `--help` at every level, and `explain` "
		"for the concepts a command cannot carry (`#154`). Printing the agent's guide beside "
		"those would be a third answer to a question that already has two, which is the defect "
		"`#154` closed. **Deleting this entry is what would close that.**",
	),
	"close": (
		"protocol",
		"Resource lifetime, not a capability §13.7 could deny anybody. `cli/personal.py` "
		"closes its clients through the context manager `opened()` wraps, which is the same "
		"call by another spelling.",
	),
}

#: Client methods the MCP adapter does not call, and why. **The list `#149` is deleting.**
NOT_IN_MCP: dict[str, Excuse] = {
	"statuses": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"create_status": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"update_status": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"delete_status": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"link_types": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"create_link_type": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"update_link_type": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"delete_link_type": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"tags": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"create_tag": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"update_tag": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"delete_tag": (
		"budget",
		"`SR#826`. Asked of `SR#484`'s test — *what would an agent get wrong without it?* — and the answer is nothing: curating a workspace's vocabulary is configuration somebody does once, not daily work, and `subroutine_call_api` reaches every one of these routes today. The surface is at **14 of 14 tools** under §21.2, so twelve more would be a raise of both the count and the byte cap for a capability an agent has no routine use for.\n\n**What would change it**: an installation whose agents genuinely invent statuses as they work, which would make this daily rather than occasional. Nothing measures that yet.",
	),
	"occurrences": (
		"budget",
		"`SR#94`, §6.7, and `SR#484`'s test rather than *is there room*: what would an agent get "
		"wrong without it? Nothing — `subroutine_show` reads the rule back as a sentence, so "
		"an agent knows a task comes round every other Tuesday, and a list of instants is what "
		"a **calendar** draws a grid from. This is a fifteenth tool for a question no agent has "
		"been measured asking.\n\n"
		"Reachable through `subroutine_call_api` meanwhile, which is what `SR#485` built it for. "
		"**What would remove it**: an agent measured needing the dates rather than the rule — "
		"scheduling work around a series, or answering *when is the next one* without doing "
		"the arithmetic itself.",
	),
	"read_repeat": (
		"budget",
		"`#94`. The argument is the CLI's and one more: `subroutine_add` and `subroutine_update` already answer with the repeat read back from the stored rule, so an agent that files one is told what it means without asking. Creating is cheap and reversible here, which is what makes *confirm afterwards* an honest substitute for *confirm first* on this surface and not on a form.\n\n"
		"Reachable through `subroutine_call_api` meanwhile, which is exactly what `#485` built it for. **What would remove it**: measured evidence of an agent filing repeats it did not mean, or enough headroom under §21.2's cap that the question stops being a trade.",
	),
	"move": (
		"budget",
		"`#44`. Asked of `#484`'s test — *what would an agent get wrong without it?* — and "
		"the answer is nothing: re-parenting is a reorganisation rather than daily work, "
		"`subroutine_update` refuses an argument it does not declare by name (`#379`) so an "
		"agent that reaches for it is told, and `subroutine_call_api` reaches "
		"`POST /v1/tasks/{ref}/move` today. That escape hatch is what `#484` built for "
		"exactly this shape of capability.\n\n"
		"**What would change it is a model in which membership is the parent link** — if a "
		"release's contents become its sub-tasks, then assembling one out of items that "
		"already exist *is* re-parenting, and it becomes something an agent does routinely "
		"rather than once. Adding the tool then is a raise of both the count and the byte "
		"cap, with the argument written into `tests/test_mcp.py` as §21.2 requires. "
		"**Deleting this entry is what would close that.**\n\n"
		"That question was `#17`, which is **in the trash** as of 2026-08-15 — so the "
		"condition is written out here rather than left as a ref, because an excuse whose "
		"trigger is an item nobody can reach is one that can never fire (`#820`'s shape).",
	),
	"count_tasks": (
		"budget",
		"`#296`. It exists so the two rename commands can print a number that is not a page "
		"size, and both of those are already `NOT_IN_MCP` — renaming is rare, consequential "
		"and confirmed at a terminal. An agent wanting a count asks `subroutine_list` and "
		"reads what came back; a whole tool schema in every session to save it that is the "
		"trade §21's budget exists to refuse.",
	),
	"create_login_link": (
		"administrative",
		"`#248`. It produces an address somebody opens in a browser, and an agent has no "
		"browser — a tool for it would spend schema in every session on a string its holder "
		"cannot use. **Not a security control, and saying so is the point**: the service "
		"gates issuing for somebody else on `instance:user_create`, exactly as issuing them "
		"a token is, so an agent holding that authority is refused or allowed identically on "
		"every surface. `#487` is what happens when a deny-list is mistaken for the check.",
	),
	"set_timezone": (
		"budget",
		"`#994`. It writes §6.5's user level, which records **where a person keeps their "
		"diary** — and an agent has no locality. Its account leaves the field null, the chain "
		"falls through to the workspace and then the instance, and that is the right answer "
		"rather than an omission. So a tool would be schema in every session for a value the "
		"caller has no way to be right about.\n\n"
		"**Not a security control, and saying so matters here more than usual**: the service "
		"refuses anybody but the account itself, and there is no permission that grants it "
		"otherwise (Simon's decision of 2026-08-18). An agent calling this through "
		"`subroutine_call_api` reaches its *own* row and nobody else's, which is the same "
		"answer this list gives. `#487` is what happens when a deny-list is mistaken for the "
		"check.\n\n"
		"**What would change it is an agent that renders dates for a person** — a scheduled "
		"summary, a digest — where the zone it should count from is the reader's rather than "
		"its own. That is a question about whose day is being described, and it wants an "
		"answer before a tool. **Deleting this entry is what would close that.**",
	),
	"sign_out_everywhere": (
		"administrative",
		"`#248`. The same tier as `set_active` beside it: ending the sessions somebody is "
		"working in is a decision about a person's access rather than about their work, and "
		"§12.4 puts it at a terminal where whoever takes it can see what they are doing.",
	),
	"transfer_agent": (
		"administrative",
		"`#478`. Deciding who answers for an agent is a person agreeing to be accountable, and "
		"an agent cannot agree on anybody's behalf — the service refuses it outright, so a tool "
		"for it would be a schema in every session for a call that always fails. Same tier as "
		"`set_active` beside it, and the same §12.4 argument. **Deleting this entry is what "
		"would close that.**\n\n"
		"Worth noting for `#427`: this arrives as a *second* capability on a route "
		"`REACHED_BY` already maps to one method, so the route-to-method map cannot express it "
		"and only this list records that it was considered."
	),
	"set_active": (
		"administrative",
		"`#475`. Marking a person as having left is an instance-tier administrative act, and "
		"§12.4 keeps those on the CLI where they work when the service does not. It is also the "
		"one act that can stop *the agent making the call* — every agent answers to a person, "
		"so an agent that could deactivate one could revoke itself and its siblings in a single "
		"call it cannot undo. Not a budget decision: this is a capability an agent should not "
		"have, and deleting this entry needs an argument about authority rather than about "
		"tool count. **Deleting this entry is what would close that.**\n\n"
		"**Since `#487` the service refuses it outright for any service account**, which is "
		"`transfer_agent`'s position beside it: a tool would be a schema in every session for a "
		"call that always fails. That is what this entry *asserted* when it was written and "
		"nothing enforced — it read as a considered exclusion while the act stood open on the "
		"two surfaces that already reached it. Reading it as a design record is what found that "
		"(decision `#484`), so it is worth saying which half is now load-bearing: the refusal "
		"is, and this entry is the note explaining why no tool is coming."
	),
	"create_workspace": (
		"disclosure",
		"`#300`. A tenancy boundary, and an instance-tier permission no role can carry — only "
		"a superuser holds `instance:workspace_create` (§7.1). An agent that made one would "
		"be creating a place its own tools then could not see into, since a session reaches "
		"one connection and one workspace at a time (`#276`). §1.4's argument runs the other "
		"way here as it does for `move_project`: harder to reach is the feature.",
	),
	"delete_workspace": (
		"disclosure",
		"`#704`. Takes a whole tenancy out of sight — every item, project, comment and "
		"credential in it — for everybody who can reach it, and it is the only verb the "
		"`owner` and `admin` roles differ by. §1.4's argument runs the other way here as it "
		"does for `rename_workspace`: this should be harder to reach, not easier. The CLI "
		"half counts what goes with it and names the members before asking, which is not a "
		"shape a tool call has. `subroutine_call_api` reaches it for the agent that genuinely "
		"means to (`#484`).",
	),
	"restore_workspace": (
		"disclosure",
		"`#704`. The other half of the pair above, and it goes with it: a tool that could "
		"undo the deletion without one that could make it is a schema in every session for "
		"a state no agent can reach.",
	),
	"rename_workspace": (
		"disclosure",
		"`#295`. Retires an address every item in the workspace is written as, for everybody "
		"who can reach it — §1.4's argument runs the other way here as it does for "
		"`move_project`: this should be harder to reach, not easier. The CLI half counts the "
		"items and the members and asks before doing any of it, which is not a shape a tool "
		"call has, and an agent that renamed a workspace would break addresses in notes it "
		"cannot see.",
	),
	"move_project": (
		"disclosure",
		"Reparenting a whole subtree: rare, consequential, and no undo. §1.4's argument runs "
		"the other way for this one — it should be harder to reach, not easier — and the CLI "
		"half (`#246`) counts what will move and asks before doing it, which is not a shape "
		"a tool call has. An eleventh tool for an operation nobody performs weekly is also "
		"the wrong side of the budget `tests/test_mcp.py` holds.",
	),
	"close": (
		"protocol",
		"Resource lifetime, not a capability §13.7 could deny anybody — the server closes "
		"its client when the process ends, which is what a stdio adapter's lifetime is.",
	),
	"discard": (
		"budget",
		"Deleting. §6.9 makes it soft and `#140` gave a person `subroutine delete`, but an "
		"agent removing items unprompted is the one write worth making it ask for — and the "
		"schema bytes buy little when the CLI is one shell call away.",
	),
	"undiscard": (
		"budget",
		"Restoring, which only matters once deleting is reachable. Same argument as "
		"`discard` above, and it would be odd to spend §13.3's bytes on the undo of a "
		"thing this surface cannot do.",
	),
	"identity": (
		"budget",
		"Which instance this is and what workspaces it holds. §21.3's server instructions "
		"already name the connection an agent is on, so a tool for this would spend schema "
		"restating a fact every session already carries.",
	),
	# `me` was excused here on 2026-08-03 and the excuse was measured false the same day
	# (`#346`, `#347`): it claimed a tool would only restate the principal the connection
	# already implies, and a shared connection name does not imply a shared principal. The
	# entry is gone rather than reworded, because deleting it is what closes the item — and
	# `subroutine_whoami` is what `test_mcp` now argues the budget for.
	"tokens": (
		"budget",
		"The credential inventory (`#348`). An agent reading which credentials exist is one "
		"question away from asking which of them can write, and that is a question for the "
		"person who issued them — `subroutine_whoami` answers the one an agent actually has, "
		"which is what *its own* credential may do.",
	),
	"issue_token": (
		"budget",
		"Minting a credential (`#348`). An agent that can issue one can issue one for a "
		"session nobody is watching, and §7.4's whole story is that a credential is narrower "
		"than the person who asked for it — an agent handing them out unprompted is the write "
		"most worth making somebody ask for, which is `discard`'s argument at a higher stake.",
	),
	"revoke_token": (
		"budget",
		"Revoking (`#348`). The undo of a write this surface cannot do, and the one whose "
		"mistake locks a person out of their own instance.",
	),
	# **The four calendar methods, and the argument is one sentence rather than four** (`#916`,
	# docs/design.md §20.3): a feed is a person subscribing their own diary to their own work,
	# and an agent has no diary. Nothing an agent does is made possible by one, which is the
	# test §21.2 asks of a fifteenth tool rather than whether there is room.
	"calendars": (
		"budget",
		"The feed inventory. §20.6 accepts that a feed URL is a bearer credential nobody can "
		"audit, so a list of somebody's is the map that makes one worth stealing — and the "
		"whole surface exists for a reader who has no calendar to point at anything.",
	),
	"create_calendar": (
		"budget",
		"Minting one (§20.3). `issue_token`'s argument at a lower stake and with a worse "
		"recovery: a feed URL is a credential that travels in a path, so it lands in a "
		"calendar application's own logs and in whatever synced it — an agent handing one "
		"out unprompted is a leak nobody is positioned to notice.",
	),
	"reset_calendar": (
		"budget",
		"Replacing a feed's URL (§20.3). The write whose *success* silently breaks somebody "
		"else's calendar, with nobody to tell — an agent should not do that on a hunch.",
	),
	"revoke_calendar": (
		"budget",
		"Stopping one (§20.3), which is `revoke_token`'s argument: the undo of a write this "
		"surface cannot make.",
	),
	"users": (
		"budget",
		"Who is on this instance (`#174`). An agent needs a name to attribute or assign work "
		"to, and it already meets those on the items it reads — a directory is what somebody "
		"does while setting a team up, which is a person's job and a shell call away.",
	),
	"create_user": (
		"budget",
		"Adding an account. `instance:user_create` is a verb no role carries, so an agent "
		"holding it is already an unusual arrangement somebody made on purpose — and they "
		"can run the command. Five administrative tools would cost §13.3's budget more than "
		"every agent that never calls them would ever get back.",
	),
	"members": (
		"budget",
		"Who belongs to a workspace (§7.3a). Same argument as `users` above: it is a question "
		"asked while arranging a team rather than while doing the work.",
	),
	"add_member": (
		"budget",
		"Deciding who may work somewhere (§7.3a). `workspace:admin`, and the one write in this "
		"group whose mistakes are visible to other people — an agent granting membership "
		"unprompted is the write worth making somebody ask for, which is `discard` above.",
	),
	"update_project": (
		"disclosure",
		"`#983` and `#434`. Carries `status`, and putting a project on hold takes its work out "
		"of *everybody's* `--ready` and off the agenda's Next — a workspace-wide effect from "
		"one tool call, with no step at which anybody is asked. §1.4's argument runs the way it "
		"does for `rename_project` directly below: this should be harder to reach, not easier. "
		"`subroutine_call_api` reaches it for the deliberate case, which is `#484`'s whole "
		"shape — an opinion, plus an escape hatch.",
	),
	"update_workspace": (
		"disclosure",
		"`#434`. Carries `timezone`, which §6.5 makes the step every date in the workspace is "
		"read through, so getting it wrong moves every deadline everybody can see. Same "
		"argument as `rename_workspace` above and for the same reason: an agent should reach "
		"this through `subroutine_call_api` having been asked, not through a tool it can call "
		"on its own. **And a second field with the same shape since `#986`**: "
		"`prioritised_project` reorders every ranked listing and agenda in the workspace, for "
		"everybody in it, and decision `#982`'s whole argument is that a focus set and forgotten "
		"is worse than none — which is exactly what a tool an agent can reach unasked would "
		"produce.",
	),
	"rename_project": (
		"budget",
		"Renaming a project (`#176`). It breaks every address anybody wrote down, which is why "
		"the CLI asks before doing it — and a tool an agent can call without being asked is the "
		"wrong shape for that. §13.3's bytes are better spent elsewhere.",
	),
	"remove_member": (
		"budget",
		"The undo of `add_member` (`#174`), and it would be odd to spend §13.3's bytes on the "
		"undo of something this surface cannot do.",
	),
}


def _declared (verbs: frozenset[str]) -> set[tuple[str, str]]:
	"""Return every mounted route using one of ``verbs``, as (verb, path)."""

	found = set()

	for path, methods in subroutine.api.routing.declarations(subroutine.api.app.ROUTERS):
		for verb in methods & verbs:
			found.add((verb, path))

	return found


def _mutating () -> set[tuple[str, str]]:
	"""Return every route that changes something."""

	return _declared(MUTATING)


def _reading () -> set[tuple[str, str]]:
	"""Return every route that answers a question."""

	return _declared(READING)


def _protocol () -> set[str]:
	"""Return every method of the client protocol."""

	return {
		name
		for name in dir(subroutine.clients.base.Client)
		if not name.startswith("_") and callable(getattr(subroutine.clients.base.Client, name))
	}


def _called_in (package: str) -> set[str]:
	"""Return the protocol methods this package calls, read from its source.

	**Derived rather than declared, which is the point.** A table saying "MCP reaches
	``link``" is a claim somebody typed; this is the source saying so. It matches on the
	attribute name alone — every call here is on a variable named for a client, and a
	protocol method name colliding with something else would produce a false *pass*, not a
	false failure, which is the safe direction for a check whose job is to find gaps.
	"""

	names = _protocol()
	found = set()

	for path in sorted((ROOT / "src" / "subroutine" / package).rglob("*.py")):
		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

		for node in ast.walk(tree):
			if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
				continue

			if node.func.attr in names:
				found.add(node.func.attr)

	return found


def test_every_mutating_route_is_either_reached_or_excused () -> None:
	"""**The check the three walls needed and did not have.**

	It would have listed all of them in one run, in the time this takes to execute, instead of
	them being found one at a time by somebody writing prose against the product.
	"""

	classified = set(REACHED_BY) | set(NOT_REACHED)
	unclassified = _mutating() - classified

	assert not unclassified, (
		f"{sorted(unclassified)} change something and nothing says whether a client can reach "
		f"them. Add the method to REACHED_BY, or a written reason to NOT_REACHED."
	)


def test_every_reading_route_is_either_reached_or_excused () -> None:
	"""The second edge, added by ``#148`` and immediately worth its keep.

	``GET /v1/tasks/{ref}/events`` had reached no client since the day it shipped, so an
	item's history was unreadable from the CLI and from MCP alike — and ``#52`` spent a
	morning putting comments into that history without anybody noticing there was nowhere to
	read it. A missing writer is work nobody can do; a missing reader is an answer nobody can
	get, and it is the quieter of the two.
	"""

	classified = set(READ_BY) | set(NOT_REACHED)
	unclassified = _reading() - classified

	assert not unclassified, (
		f"{sorted(unclassified)} answer a question and nothing says whether a client can "
		f"reach them. Add the method to READ_BY, or a written reason to NOT_REACHED."
	)


def test_every_client_method_is_either_called_by_the_cli_or_excused () -> None:
	"""The third edge, and the half a route-level check structurally cannot see.

	A capability can reach ``clients/base.Client`` and stop there, and then both of the
	surfaces a person and an agent actually touch are missing it while every earlier check
	passes.
	"""

	unclassified = _protocol() - _called_in("cli") - set(NOT_IN_CLI)

	assert not unclassified, (
		f"the CLI never calls {sorted(unclassified)}, and nothing says why. Call it, or add a "
		f"written reason to NOT_IN_CLI."
	)


def test_every_client_method_is_either_called_by_mcp_or_excused () -> None:
	"""The same for the agent's surface, where the budget makes the decision a real one.

	``#145`` lived here: ``comment`` reached the protocol correctly and the MCP adapter called
	it wrongly. This does not catch that — the call was there, with the wrong argument — which
	is the limit written into this module's docstring rather than left to be discovered.
	"""

	unclassified = _protocol() - _called_in("mcp") - set(NOT_IN_MCP)

	assert not unclassified, (
		f"the MCP adapter never calls {sorted(unclassified)}, and nothing says why. Add a "
		f"tool, or a written reason to NOT_IN_MCP."
	)


def test_no_list_names_a_route_that_no_longer_exists () -> None:
	"""The other direction, and the one that lets an entry be deleted with confidence.

	Without it a stale exemption outlives the endpoint it excused, and the next reader has no
	way to tell a decision from a leftover. Three separate allow-lists rotted this way on
	2026-07-31, which is why every list here has this check rather than only the first one.
	"""

	live = _mutating() | _reading()
	stale = (set(REACHED_BY) | set(READ_BY) | set(NOT_REACHED)) - live

	assert not stale, f"{sorted(stale)} are listed here and are not routes"


def test_no_list_names_a_client_method_that_no_longer_exists () -> None:
	"""The same, for the three lists keyed by protocol method rather than by route."""

	declared = _protocol()
	named = set(REACHED_BY.values()) | set(READ_BY.values())
	excused = set(NOT_IN_CLI) | set(NOT_IN_MCP)

	assert named <= declared, f"{sorted(named - declared)} are not methods of Client"
	assert excused <= declared, f"{sorted(excused - declared)} are excused and do not exist"


def test_nothing_is_excused_from_a_surface_that_reaches_it () -> None:
	"""An exemption for something already built is the failure mode nobody notices.

	It reads as a decision and is a leftover, and the surface it excuses is *working* — so
	nothing else in this file, and nothing a user does, will ever contradict it. Deleting the
	entry is what closes the item it names, and this is what says the moment has come.
	"""

	for package, excuses in (("cli", NOT_IN_CLI), ("mcp", NOT_IN_MCP)):
		reached = _called_in(package) & set(excuses)

		assert not reached, (
			f"{sorted(reached)} are excused from {package} and are called there. Delete the "
			f"entry — and the item it names may be done."
		)


def test_every_reason_is_a_reason () -> None:
	"""An exemption list is only worth having while the entries say something.

	The failure mode is a one-word "later" that reads as considered and is not, and it arrives
	the first time somebody is in a hurry — which is exactly when the endpoint they are adding
	deserves the thought. ``#148`` added the *kind*, so that an exemption has to name which of
	the five constraints it is claiming rather than describing one in passing.
	"""

	lists: tuple[tuple[str, dict[typing.Any, Excuse]], ...] = (
		("NOT_REACHED", typing.cast(dict[typing.Any, Excuse], NOT_REACHED)),
		("NOT_IN_CLI", typing.cast(dict[typing.Any, Excuse], NOT_IN_CLI)),
		("NOT_IN_MCP", typing.cast(dict[typing.Any, Excuse], NOT_IN_MCP)),
	)

	for name, excuses in lists:
		for subject, (kind, reason) in excuses.items():
			assert kind in KINDS, f"{name}[{subject}] claims {kind!r}, which is not a kind"
			assert len(reason) > 40, (
				f"{name}[{subject}] is excused by {reason!r}, which explains nothing"
			)

			if kind == "tracked":
				assert "`#" in reason, (
					f"{name}[{subject}] is 'tracked' and names no item, so nothing tracks it "
					f"and 'later' is doing the work a decision should"
				)

			else:
				assert "§" in reason or "`#" in reason, (
					f"{name}[{subject}] names neither a specification section nor an item"
				)


def test_the_cli_and_mcp_agree_about_what_a_ref_names () -> None:
	"""``#145``, guarded where it can be: both surfaces take a document's number.

	One counter per workspace serves tasks and documents (§6.2), so a ref alone does not say
	which it is. The MCP adapter assumed "task" in one tool and asked in another, three
	hundred lines apart — the shape a per-method check cannot see, so it is asserted directly.
	"""

	source = (ROOT / "src" / "subroutine" / "mcp" / "tools.py").read_text(encoding="utf-8")

	assert source.count("_item(client") >= 2, (
		"the MCP tools that take a ref must resolve it the same way; `_item` is that way, and "
		"fewer than two callers means one of them has its own answer again"
	)


def test_the_skill_does_not_teach_around_a_gap_silently () -> None:
	"""What an inconsistency costs, made visible before somebody pays it again.

	The skill tells an agent to shell out to the CLI for what MCP cannot do. That is a
	legitimate answer — the plugin ships both — but each one is a place the agent surface
	routes around itself, and on 2026-08-01 there were four. This pins the count so that
	adding a fifth is a decision rather than a drift, and `#149` lowers it.
	"""

	skill = (ROOT / "plugins" / "subroutine" / "skills" / "subroutine" / "SKILL.md").read_text(
		encoding="utf-8"
	)

	# **Counted against the real command list, because the word after "subroutine " is not
	# always a command** (`#237`). This split on the literal and took the next token, so
	# ``subroutine init`` in prose and the same in a fenced block counted as two — the second
	# carrying its closing backtick — and a line reading ``uv tool install subroutine  # or:
	# pipx …`` registered a command called ``#``. Three spellings of one route-around and one
	# that was not a route-around at all, against a budget of three: it failed on a change that
	# added no shelling out whatsoever, which is a guard measuring its own regex.
	registered = {
		command.name or (command.callback.__name__ if command.callback else "")
		for command in subroutine.cli.main.app.registered_commands
	} | {group.name for group in subroutine.cli.main.app.registered_groups if group.name}

	# **Across a line break, and that was not a detail** (`#540`). Written as a literal space,
	# this scan missed `subroutine explain` for as long as the paragraph naming it happened to
	# wrap between the two words — so the ceiling below read 5 while the skill named 6, and
	# rewrapping one sentence "added" a command nobody had added. A guard whose answer depends on
	# where Markdown wrapped is not measuring the thing it claims to.
	#
	# This is the second time a scan over this file has been defeated by a line break; the first
	# was the project-key rule, where a wrap fell between "32" and "characters".
	commands = {
		match.group(1)
		for match in re.finditer(r"\bsubroutine\s+([a-z][a-z-]*)", skill)
		if match.group(1) in registered
	}

	# The floor matters as much as the ceiling here: an extraction that reaches nothing passes
	# a ceiling test silently, which is the failure this comment exists because of.
	assert commands, "found no CLI commands in the skill — has this stopped reaching them?"

	# **Four since `#336`, and the fourth was argued for rather than absorbed.** `whoami` is a
	# route-around by this test's definition and is the right one: an agent that can run a shell
	# has two credentials available to it — the one this server was given and the one the shell
	# resolves — and the *only* way to find out which the shell will use is to ask the shell.
	# A tool here would answer for the server, which is the half that was never in doubt.
	#
	# This used to end "the reason is written out in NOT_IN_MCP under `me`", and that entry was
	# deleted in the same commit — correctly, since deleting an excuse is what closes it. So
	# the pointer outlived the thing it pointed at, by minutes (`#361`). The reason is above.
	# **Raised to 5 for `#485`**, deliberately and with the reason here rather than in a commit
	# message. The addition is `doc`: `subroutine doc edit 42` is how a document is revised, and
	# the skill now says so because `#293` measured what its absence cost — an agent concluded
	# documents were immutable and stopped filing them, giving one-item-in-one-place, which is
	# this project's own principle, as the reason.
	#
	# It is not a gap being taught around, which is what this guard exists to catch. The route
	# is reachable from MCP through `subroutine_call_api`; what the skill names is the *better*
	# way for an agent that has a shell, which is the sentence `#480` is about. The distinction
	# worth keeping: teaching a shell-out for something MCP cannot do is a gap, and teaching one
	# for something it can do more clumsily is advice.
	# **Raised to 6 for `#540`, and nothing was added to the skill to justify it.** `explain` had
	# been named all along and the scan above could not see it across a line break, so the
	# ceiling has been one too low since `#485`. Correcting the regex is what surfaced it.
	#
	# It stays a route-around this guard should tolerate, for the same reason as `doc`: `explain`
	# teaches a *person* the grammars — dates, capture, refs — and an agent that has a shell can
	# read them faster than it can be told them. What it is not is a gap, because
	# `subroutine://docs/agent` and `subroutine://meta` carry the same vocabulary to an agent
	# that has no shell at all. Teaching a shell-out for something MCP cannot do is a gap;
	# teaching one for something it can do more clumsily is advice.
	# **Unchanged at 6 for `#822`, and the composition changed under it, which is worth saying
	# rather than sliding through.** `doc` left: `subroutine_document` takes a `ref` now, so
	# naming `subroutine doc edit 42` would be teaching a shell-out for something this surface
	# does directly — the one thing this guard exists to catch, arrived at by *closing* a gap
	# rather than by adding a sentence. `token` took the slot, and it is the stronger kind of
	# entry: `token create` is genuinely unreachable from here (`#484`), where `doc` and
	# `explain` are the softer "better with a shell" case.
	assert len(commands) <= 6, (
		f"the skill sends an agent to the CLI for {sorted(commands)}. Each is something MCP "
		f"cannot do; if that is right, say so in NOT_IN_MCP and raise this number deliberately"
	)


def test_no_route_is_both_reached_and_excused () -> None:
	"""**The gap that let three excuses outlive their reason.**

	``NOT_REACHED`` fails the build for a route that is *neither* reached nor excused, which
	is the case it was written for. It says nothing about a route that is *both* — so when a
	client method finally arrived, the entry claiming nothing reached it simply stayed, still
	naming an item, still reading as a considered decision.

	Three of them at once on 2026-08-02: `#291` reached ``PATCH /v1/documents``, `#295`
	reached ``PATCH /v1/workspaces`` and `#300` reached ``POST /v1/workspaces``, and this file
	went on saying all three were gaps waiting on `#141`. An allow-list that cannot go stale
	is the whole value of writing reasons down; one that can is a document with a test's
	reputation.
	"""

	contradicted = sorted(
		f"{method} {path}"
		for method, path in set(NOT_REACHED) & (set(REACHED_BY) | set(READ_BY))
	)

	assert not contradicted, (
		f"these routes are excused as unreachable and are reached: {contradicted}. "
		f"Delete the entry — that is what closes the item it names."
	)


def test_no_client_method_is_both_called_by_mcp_and_excused_from_it () -> None:
	"""The same check one surface along, so the two lists cannot drift apart either."""

	contradicted = sorted(set(NOT_IN_MCP) & _called_in("mcp"))

	assert not contradicted, (
		f"these methods are excused from MCP and called by it: {contradicted}."
	)


def test_every_denied_route_is_one_that_exists () -> None:
	"""`#485`'s deny-list may not name a route that has gone.

	The three entries in :data:`subroutine.mcp.tools.DENIED` are the shape `#412` keeps finding:
	written once with the argument fresh, then never re-read. An entry naming a route that no
	longer exists still *reads* as a considered exclusion — it refuses nothing and looks like a
	control — which is the same failure as an allow-list entry whose reason has expired.

	Imported rather than restated, so the surface and this guard cannot come to disagree about
	which routes are refused.

	**An equality since `#528`, where it used to be a regex match against a route with its
	parameters substituted.** The entries are now the path templates the application registers,
	so "does this entry name a real route" is the same question the router answers rather than a
	second one asked with a pattern — which is what let three respellings of a denied path walk
	past the surface this backs up.
	"""

	mounted = {
		(method, path)
		for path, methods in subroutine.api.routing.declarations(subroutine.api.app.ROUTERS)
		for method in methods
	}

	for verb, template, instead in subroutine.mcp.tools.DENIED:
		assert (verb, template) in mounted, (
			f"{verb} {template} is refused by subroutine_call_api and is not a route this "
			f"application registers, so it guards nothing"
		)
		assert instead, f"{verb} {template} refuses without naming what to do instead"


def test_every_route_that_answers_with_a_credential_is_denied_to_an_agent () -> None:
	"""`#927`'s H-7, and the direction the deny-list could not answer for itself.

	`DENIED` had three entries against roughly forty-three writing routes, and its written
	reason covered one kind of risk: consequential, no undo, a confirmation step. Two routes
	sat outside it that fail differently — `POST /v1/tokens` answers with a secret that exists
	nowhere else ever, and `POST /v1/login-links` with a working sign-in URL that takes a
	`username`, so it can be minted for somebody else. **A tool result is text in a model's
	context**, which is the one place this project cannot revoke anything from.

	Not an escalation: `_refuse_amplification` correctly stops a credential widening itself.
	A disclosure — and `api/mcp.py` already argues that this transport must refuse browser
	sessions because it is *"driven by an agent reading item text that anybody with a
	credential may have written"*. The same reader must not be handed a credential.

	**Derived from the response models rather than from the two paths.** A list of routes that
	return a secret is a second copy of a fact the routes already carry, and the copy is the
	one that goes stale — so this asks which routes answer with one of
	`tools.CARRIES_A_SECRET` and requires each to be refused. A third such route cannot be
	added without somebody deciding about this.

	The floor matters as much as the check: if the response models stopped being readable this
	would find nothing and pass, which is the "reads nothing and succeeds" shape met three
	times in this repository already.
	"""

	minting = {
		(method, f"{prefix}{route.path}")
		for prefix, router in subroutine.api.app.ROUTERS
		for route in typing.cast(list[typing.Any], router.routes)
		for method in sorted(getattr(route, "methods", None) or ())
		if getattr(route, "response_model", None) in subroutine.mcp.tools.CARRIES_A_SECRET
	}

	assert len(minting) >= 2, (
		f"only {len(minting)} routes were found to answer with a credential — has the "
		f"response model stopped being readable from the route?"
	)

	refused = {(verb, template) for verb, template, _instead in subroutine.mcp.tools.DENIED}
	reachable = minting - refused

	# A `HEAD` or a `GET` on one of these would be a different question and there are none;
	# every one found is a mint.
	assert not reachable, (
		f"these routes answer with a live credential and subroutine_call_api can reach them: "
		f"{sorted(reachable)}. A tool result is text in a model's context."
	)


# --- Fields, not only methods (`#427`) --------------------------------------------------
#
# **The guard above answers "can a client call this route", and cannot answer "can a client
# pass this argument".** A capability arriving as a field on a route both surfaces already
# reach is invisible to it by construction — which `#149` recorded in July and which has since
# cost `#178`, `#367`, `#392`, `#424` and `#491`. Every one was found by somebody using the
# product; none by the suite.
#
# **The map is derived, not maintained.** `clients/http.py` names the paths it calls, so
# reading its own `_json("VERB", "/path")` calls gives route -> {methods} with nothing to keep
# in step. That removes the naming-mismatch hazard the design note worried about, and it is
# what `#336` should have had: a hand-written map was transposed there, consistently, so every
# check passed.
#
# It also corrects an assumption in `REACHED_BY`: `PATCH /v1/tasks` is reached by **both**
# `update` and `schedule`, and that map records one method per route. The union is what a field
# check needs, or it reports gaps that are not there.

#: Where a request field and a client keyword are the same capability under two names.
#:
#: **Unavoidable and deliberately small.** The API is explicit where a client is conversational
#: — `workspace_id` against `workspace` — and each entry here is a claim that two spellings mean
#: one thing. Every one was checked by reading both sides; a wrong entry hides a real gap, which
#: is the failure mode of a map rather than of a scan.
SPELLED_DIFFERENTLY = {
	"workspace_id": {"workspace"},
	"is_active": {"active"},
	"responsible": {"to"},
	"project_scope": {"projects"},
	"project_write_scope": {"writes"},
	# `plan` is the MCP tool's word for it and `starts` is the request field (`#854`).
	"starts": {"starts", "plan"},
	"snooze": {"snooze", "defer"},

	#: `GET /v1/changes` declares `actor_filter` and the client spells it `mine`. Checked on
	#: both sides rather than assumed: `api/changes.py` compares it against `ACTOR_ME` and
	#: passes `mine=actor_filter == ACTOR_ME`, and the client sends `actor="me" if mine`.
	#: One capability, and the only filter `#501`'s guard reported that is not a gap.
	# The body field is `from`, which is a Python keyword, so the model aliases it `from_`
	# and the client — having no such constraint on a *parameter* name — calls it `start`.
	"from_": {"start"},
	#: `mine` is the client's argument and `actor` is what goes on the wire; the route
	#: declares `actor_filter`. All three are one capability, and `#712` made the wire
	#: name the one that has to be listed.
	"actor_filter": {"mine", "actor"},

	#: The body field is `parent_task_id` and both clients call the argument `parent`,
	#: which is what `move` has always called the same thing. `#510` widened the field
	#: to take a ref rather than renaming it, because renaming it is a breaking wire
	#: change; the client offers the name a caller would reach for either way.
	"parent_task_id": {"parent"},
}

#: Fields `POST /v1/tasks` accepts that §6.13's capture line sets instead.
#:
#: **Not a gap and not an excuse: a different way in.** `capture(text=…)` parses these out of one
#: string, which is the documented primary path (§1.4) and the grammar the skill teaches. Listing
#: them as unreachable would be measuring the API's shape rather than the product's.
#: **`assignee_id` is in this set because `@si` works, which was checked by running it** — the
#: first version of this list was written by reading the request models, and omitting it made
#: this guard report a gap on `POST /v1/tasks` that does not exist. A missing entry in an
#: exclusion list manufactures a false gap exactly as convincingly as a wrong one hides a real
#: one, and both read identically from inside.
BY_THE_CAPTURE_GRAMMAR = frozenset({
	"title", "importance", "urgency", "estimate", "tags", "due", "snooze", "status",
	"starts", "due_is_all_day", "starts_is_all_day", "snoozed_is_all_day", "timezone",
	"assignee",
})

#: A request field no client passes, and why. Same discipline as every list here: a written
#: reason, and something that makes the entry go away.
UNREACHED_FIELDS: dict[str, Excuse] = {
	"expected_version": (
		"tracked",
		"`#494`. §8.9's concurrency check is built, tested and reachable only over raw HTTP — "
		"five routes accept it and no client method passes it. Filed rather than excused, "
		"because 'opt-in by design' explains why a *person* never meets it and not why a "
		"*client* cannot offer it. **Deleting this entry is what closes `#494`.**",
	),
	"owner_id": (
		"unbuilt",
		"Reassigning what somebody owns is not a capability any surface offers, on purpose: "
		"§7.3a hangs private-project membership on ownership, so moving it silently changes "
		"who can see a tree. It wants the same shape as `users.transfer` — a person agreeing "
		"to take it on — rather than a field. No item: nobody has asked, and filing one would "
		"be inventing a requirement.",
	),
	"supersedes": (
		"unbuilt",
		"§5.10's document supersession. The column exists and the route accepts it; nothing "
		"reads it and no surface writes it. The inert-control family (`#247`, `#251`, `#303`) "
		"one step earlier — declared and unwired — and `#303`'s lesson was that deleting beat "
		"wiring. Which of the two this is has not been decided.",
	),
	"parent": (
		"unbuilt",
		"Filing a document under another document. `create_project` takes a `parent` and this "
		"is the document equivalent; nothing has ever asked for a document tree, and §5.6a "
		"says a feature is just a parent *item*, which is a link rather than a field.",
	),
	"template": (
		"unbuilt",
		"Project templates (§6.7's neighbourhood). Accepted by the route and implemented "
		"nowhere below it.",
	),
	"description": (
		"reachable another way",
		"`POST /v1/workspaces` only, and this entry is narrower than the one it replaces. The "
		"old one named `#434` and covered both halves; `#983` gave `update_workspace` a "
		"`description`, so **changing** one is reached now and only **naming one at creation** "
		"is not. `create_workspace` takes slug, title and timezone, and a caller who wants a "
		"description writes it with the next call rather than being unable to — so nothing is "
		"stuck here, which is what made the update half a bug and leaves this a rough edge.",
	),
	"settings": (
		"disclosure",
		"A workspace's settings blob. Deliberately not a client argument: it is a JSON column "
		"whose keys are undocumented and whose validation is per key, so offering it as an "
		"opaque dictionary would be a surface nobody can use correctly. A named setting gets a "
		"named argument when there is one worth having.",
	),
}


def _shaped (path: str) -> str:
	"""Return a path with every parameter flattened, so two spellings of one route compare.

	``/v1/tasks/{id_or_ref}`` and the client's ``f"/v1/tasks/{ref}"`` are the same route under
	two names for the same segment, and an f-string carrying a call — ``{_plural(entity_type)}``
	— is a segment this cannot know either. Flattening both sides is what lets them meet.
	"""

	out: list[str] = []
	depth = 0

	for character in path:
		if character == "{":
			depth += 1

			if depth == 1:
				out.append("*")

		elif character == "}":
			depth -= 1

		elif depth == 0:
			out.append(character)

	return "".join(out)


def reached_routes (source: str) -> dict[tuple[str, str], set[str]]:
	"""Return route -> client methods, read out of the HTTP client's own calls.

	Takes the source as an argument for `#405`'s reason: a scanner that cannot be handed a
	subject can only be tested against itself, and this one is satisfied most comfortably by
	reading nothing at all.
	"""

	found: dict[tuple[str, str], set[str]] = {}

	for node in ast.walk(ast.parse(source)):
		if not isinstance(node, ast.FunctionDef):
			continue

		for inner in ast.walk(node):
			if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
				continue

			if inner.func.attr not in {"_json", "_text", "_call"} or len(inner.args) < 2:
				continue

			verb, path = inner.args[0], inner.args[1]

			if not isinstance(verb, ast.Constant) or not isinstance(verb.value, str):
				continue

			if isinstance(path, ast.Constant):
				literal = str(path.value)

			elif isinstance(path, ast.JoinedStr):
				literal = "".join(
					str(piece.value) if isinstance(piece, ast.Constant) else "{x}"
					for piece in path.values
				)

			else:
				continue

			found.setdefault((verb.value, _shaped(literal)), set()).add(node.name)

	return found


def sent_by (source: str) -> dict[str, set[str]]:
	"""Return client method -> the names it actually puts on a query, from its own calls.

	**What `clients/http.py` does, rather than what `clients/base.py` declares, and that
	distinction is the whole of `#712`.** :func:`_accepted_by` reads the abstract signature, so
	a name present there satisfies it whatever either implementation does — and the base class
	is the one place that cannot send anything at all.

	Measured when it was found: removing **only** the line that puts ``status_category`` on the
	wire, leaving every signature intact, left the filter accepted from callers, dropped before
	the request, and both this file and the equivalence suite green. A caller asked for finished
	work, got an unfiltered listing, and nothing anywhere reported it.

	Every keyword of every call inside the method counts rather than only ``_given``'s. Query
	names are spread across ``_given``, ``_dated`` and the request helpers, and a whitelist of
	helper names is a second list to fall behind — where a stray non-filter keyword such as
	``params`` simply never matches a declared filter and costs nothing.

	**Used for filters and deliberately not for body fields.** A body is often a dict literal or
	a ``**`` splat built somewhere else in the method, so this would report 28 fields as
	unreachable that a client passes perfectly well — a guard that cries wolf gets an excuse
	list rather than a fix. `#712` is the filter half; the body half needs a different scan and
	is still reading signatures.

	Takes the source as an argument for `#405`'s reason, like :func:`reached_routes` beside it.
	"""

	found: dict[str, set[str]] = {}

	for node in ast.walk(ast.parse(source)):
		if not isinstance(node, ast.FunctionDef):
			continue

		names = found.setdefault(node.name, set())

		for inner in ast.walk(node):
			if isinstance(inner, ast.Call):
				names.update(
					keyword.arg for keyword in inner.keywords if keyword.arg is not None
				)

	return found


#: What each of the HTTP client's methods puts on a query, read once. `#712`.
_SENT = sent_by(pathlib.Path(subroutine.clients.http.__file__).read_text(encoding="utf-8"))


def _sent_by (method: str) -> set[str]:
	"""Return the query names one client method actually sends."""

	return _SENT.get(method, set())


def _accepted_by (method: str) -> set[str]:
	"""Return the keyword arguments one client method takes."""

	declared = getattr(subroutine.clients.base.Client, method, None)

	if declared is None:
		return set()

	return {
		name
		for name, parameter in inspect.signature(declared).parameters.items()
		if parameter.kind is inspect.Parameter.KEYWORD_ONLY
	}


def unreached_fields () -> dict[str, set[tuple[str, str]]]:
	"""Return each request field no client method can pass, and the routes accepting it."""

	source = pathlib.Path(subroutine.clients.http.__file__).read_text(encoding="utf-8")
	reached = reached_routes(source)
	found: dict[str, set[tuple[str, str]]] = {}

	for path, verbs, route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS):
		fields = subroutine.api.problems.body_fields(route)

		if not fields:
			continue

		for verb in verbs:
			methods = reached.get((verb, _shaped(path)), set())

			if not methods:
				continue

			accepted: set[str] = set()

			for method in methods:
				accepted |= _accepted_by(method)

			for field in fields:
				if field in accepted or field in SPELLED_DIFFERENTLY.get(field, set()):
					continue

				if SPELLED_DIFFERENTLY.get(field, set()) & accepted:
					continue

				if path == "/v1/tasks" and field in BY_THE_CAPTURE_GRAMMAR:
					continue

				found.setdefault(field, set()).add((verb, path))

	return found


def test_every_request_field_is_reachable_or_excused () -> None:
	"""`#427`. A field a route accepts and no client can pass is a capability nobody has.

	Five defects of this exact shape in six weeks, every one found by somebody using the
	product: `#178`, `#367`, `#392`, `#424`, `#491`. The guard beside this one cannot see them,
	because it asks whether a *route* is reached and these are all routes reached richly.

	On its first real run this found `#493` — no surface can assign a task, which is the centre
	of decision `#473` — and `#494`, §8.9's concurrency check reachable only over raw HTTP.
	"""

	found = unreached_fields()
	unexplained = sorted(field for field in found if field not in UNREACHED_FIELDS)

	assert not unexplained, (
		f"no client method passes {unexplained}, and nothing says why. Give a client the "
		f"argument, or add an entry to UNREACHED_FIELDS with a written reason — and if it is a "
		f"gap rather than a decision, file it and say so."
	)


def test_the_field_scan_actually_read_the_client () -> None:
	"""A scan that reaches nothing reports no gaps and passes, which is the failure to prevent.

	Falsified by breaking the walk rather than by trusting it: this is the floor `#405` asks
	for, and it is what the design note's own first measurement failed — walking ``app.routes``
	found 8 routes against 61 declared, so the first run said "zero excuses needed".
	"""

	source = pathlib.Path(subroutine.clients.http.__file__).read_text(encoding="utf-8")
	reached = reached_routes(source)

	assert len(reached) >= 30, (
		f"read {len(reached)} route/method pairs out of clients/http.py — has the walk stopped "
		f"reaching them?"
	)
	assert ("PATCH", "/v1/tasks/*") in reached, "the route this guard was written for is missing"
	assert reached[("PATCH", "/v1/tasks/*")] >= {"update", "schedule"}, (
		"the union is the point: one method per route is what REACHED_BY records and what "
		"makes a field check report gaps that are not there"
	)


def test_no_field_is_both_reachable_and_excused () -> None:
	"""What makes an entry go away, asked of this list like every other one here (`#405`)."""

	found = unreached_fields()
	stale = sorted(field for field in UNREACHED_FIELDS if field not in found)

	assert not stale, (
		f"{stale} is excused and no longer unreachable — a client can pass it now, so delete "
		f"the entry and close whatever it names"
	)


def test_a_field_nothing_passes_would_be_caught () -> None:
	"""Feed the real comparison a defect through its own entry point, not a copy of its rule."""

	planted = reached_routes(
		'class Client:\n'
		'\tdef update (self, *, ref, title):\n'
		'\t\treturn self._json("PATCH", f"/v1/tasks/{ref}", json={})\n'
	)

	assert planted == {("PATCH", "/v1/tasks/*"): {"update"}}, planted

	# And the real one must be reporting something, or the assertion above proves only that the
	# parser works on three lines of synthetic source.
	assert unreached_fields(), (
		"the field scan reports nothing at all, which would make the excuse list vacuous"
	)


# --- Filters, which are the third question (`#501`) -------------------------------------
#
# **Reach has three questions and this repository was asking two.** Does a client call the
# route at all — the guard at the top of this file, and `#141`. Does it pass every field the
# *body* accepts — `#427`, above. **Does it pass every filter the *query* accepts** — nothing
# asked, and the answer was fifteen.
#
# The shape is `#427`'s exactly, and deliberately so: the same derived route map, the same
# `SPELLED_DIFFERENTLY`, the same discipline of a written reason that has something making it
# go away. What differs is where the names come from — a route's `dependant.query_params`
# rather than its body model — and that listing filters have a shared vocabulary of their own
# which is nobody's capability.

#: Query parameters that are not filters and are excluded before anything is compared.
#:
#: **Paging and shaping, which belong to the transport rather than to the caller's question.**
#: `limit` and `workspace` reach the clients under their own names and are handled by
#: `SPELLED_DIFFERENTLY`; `cursor`, `fields`, `format` and `include_total` are §8.4's and
#: §14.10's machinery, deliberately not offered as client arguments — a client returns a list
#: and pages for you. Listing them as gaps would measure the HTTP surface rather than the
#: product's.
NOT_A_FILTER = frozenset({"cursor", "fields", "format", "include_total", "limit"})

#: A listing filter no client passes, and why.
UNREACHED_FILTERS: dict[str, Excuse] = {
	"include": (
		"deliberate",
		"§8.5's relation loader — `?include=backlinks` and friends — which decides how much of "
		"a *response* is assembled, not which rows are in it. A client that wanted backlinks "
		"would grow a method for them rather than a keyword here, the way `links` already did. "
		"Nothing tracks it because there is nothing missing.",
	),
}


def _query_names (route: typing.Any) -> set[str]:
	"""Return the filter names one route declares, with paging and shaping removed."""

	dependant = getattr(route, "dependant", None)

	if dependant is None:
		return set()

	return {field.name for field in dependant.query_params} - NOT_A_FILTER


def unreached_filters () -> dict[str, set[tuple[str, str]]]:
	"""Return each listing filter no client method can pass, and the routes accepting it."""

	source = pathlib.Path(subroutine.clients.http.__file__).read_text(encoding="utf-8")
	reached = reached_routes(source)
	found: dict[str, set[tuple[str, str]]] = {}

	for path, verbs, route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS):
		if "GET" not in verbs:
			continue

		names = _query_names(route)

		if not names:
			continue

		methods = reached.get(("GET", _shaped(path)), set())

		if not methods:
			continue

		accepted: set[str] = set()

		for method in methods:
			# **What it sends, not what it declares** (`#712`).
			accepted |= _sent_by(method)

		for name in names:
			if name in accepted or SPELLED_DIFFERENTLY.get(name, set()) & accepted:
				continue

			found.setdefault(name, set()).add(("GET", path))

	return found


def test_every_listing_filter_is_reachable_or_excused () -> None:
	"""`#501`. A filter the endpoint declares and no client offers is a capability, not a feature.

	The failure it is written for: `GET /v1/tasks?assignee_id=` has existed since M1, so *"what
	is assigned to whom"* — the question decision `#473`'s whole delegation model exists to
	answer — was reachable only by an agent that knew the filter existed, held a UUID rather
	than a username, and passed an explicit workspace. A person had no route at all.
	"""

	found = unreached_filters()
	unexplained = sorted(name for name in found if name not in UNREACHED_FILTERS)

	assert not unexplained, (
		f"{unexplained} is declared by a listing a client already calls and no client method "
		f"passes it. Give the method the keyword, or add an entry to UNREACHED_FILTERS with a "
		f"written reason — and if it is a gap somebody should fix, file it and say so here. "
		f"Where it reaches: { {name: sorted(found[name]) for name in unexplained} }"
	)


def test_the_filter_scan_actually_read_the_routes () -> None:
	"""A floor, because a scan that reads nothing makes every excuse above look considered."""

	listings = [
		path
		for path, verbs, route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS)
		if "GET" in verbs and _query_names(route)
	]

	assert len(listings) > 5, (
		f"only {len(listings)} routes were found to declare any filter, which means this scan is "
		f"reading almost nothing and every entry in UNREACHED_FILTERS is vacuous"
	)


def test_the_wire_scan_reads_what_a_method_sends_not_what_it_declares () -> None:
	"""`#712`, driven through :func:`sent_by`'s own entry point rather than against itself.

	`#405`'s rule: a guard is tested by handing it the defect, not by re-implementing its rule
	beside it. The subject below is a method that **declares** two filters and **sends** one —
	which is exactly the shape that stayed green for the whole of this file before, because
	`_accepted_by` read the abstract base class and a signature is where the declaration lives.

	The floor matters as much as the exclusion: a scan that read nothing would satisfy
	``"status_category" not in sent`` perfectly, and reading nothing is the failure this file
	has met more than once.
	"""

	source = "\n".join((
		"class Client:",
		"\tdef tasks (self, *, status_category=None, q=None):",
		'\t\treturn self._json("GET", "/v1/tasks", params=_given(q=q))',
	))

	sent = sent_by(source)

	assert "tasks" in sent, "the scan did not find the method at all"
	assert "q" in sent["tasks"], "the scan read nothing it was handed"
	assert "status_category" not in sent["tasks"], (
		"a filter the method declares and never sends is being counted as reached"
	)


def test_no_filter_is_both_reachable_and_excused () -> None:
	"""What makes an entry go away, asked of this list like every other one here (`#405`)."""

	found = unreached_filters()
	stale = sorted(name for name in UNREACHED_FILTERS if name not in found)

	assert not stale, (
		f"{stale} is excused and no longer unreachable — a client can pass it now, so delete "
		f"the entry and close whatever it names"
	)
