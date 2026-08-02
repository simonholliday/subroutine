"""What one surface can do and another cannot — SPEC.md §13.7, items ``#139`` and ``#148``.

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
import pathlib
import re
import typing

import subroutine.api.app
import subroutine.api.routing
import subroutine.cli.main
import subroutine.clients.base
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
	("PATCH", "/v1/tasks/{id_or_ref}"): "update",
	("POST", "/v1/tasks/{id_or_ref}/complete"): "complete",
	("POST", "/v1/tasks/{id_or_ref}/comments"): "remark",
	("POST", "/v1/documents"): "create_document",
	("POST", "/v1/users"): "create_user",
	("POST", "/v1/workspaces/{id_or_slug}/members"): "add_member",
	("DELETE", "/v1/workspaces/{id_or_slug}/members/{username}"): "remove_member",
	("POST", "/v1/documents/{id_or_ref}/comments"): "remark",
	("POST", "/v1/workspaces"): "create_workspace",
	("PATCH", "/v1/workspaces/{id_or_slug}"): "rename_workspace",
	("PATCH", "/v1/documents/{id_or_ref}"): "update_document",
	("POST", "/v1/projects"): "create_project",
	("PATCH", "/v1/projects/{id_or_key}"): "rename_project",
	("POST", "/v1/projects/{id_or_key}/move"): "move_project",
	("POST", "/v1/projects/{id_or_key}/comments"): "remark",
	("DELETE", "/v1/tasks/{id_or_ref}"): "discard",
	("DELETE", "/v1/documents/{id_or_ref}"): "discard",
	("POST", "/v1/tasks/{id_or_ref}/restore"): "undiscard",
	("POST", "/v1/documents/{id_or_ref}/restore"): "undiscard",
	("POST", "/v1/tasks/{id_or_ref}/links"): "link",
	("DELETE", "/v1/tasks/{id_or_ref}/links/{link_id}"): "unlink",
	("POST", "/v1/documents/{id_or_ref}/links"): "link",
	("DELETE", "/v1/documents/{id_or_ref}/links/{link_id}"): "unlink",
}

#: Reading routes, and the method that reaches each.
READ_BY: dict[tuple[str, str], str] = {
	("GET", "/v1/agenda"): "agenda",
	("GET", "/v1/tasks"): "tasks",
	("GET", "/v1/tasks/{id_or_ref}"): "task",
	("GET", "/v1/tasks/{id_or_ref}/comments"): "comments",
	("GET", "/v1/tasks/{id_or_ref}/links"): "links",
	("GET", "/v1/documents"): "documents",
	("GET", "/v1/documents/{id_or_ref}"): "document",
	("GET", "/v1/documents/{id_or_ref}/comments"): "comments",
	("GET", "/v1/documents/{id_or_ref}/links"): "links",
	("GET", "/v1/projects"): "projects",
	("GET", "/v1/projects/{id_or_key}/comments"): "comments",
	("GET", "/v1/tasks/{id_or_ref}/events"): "history",
	("GET", "/v1/changes"): "changes",
	("GET", "/v1/documents/{id_or_ref}/events"): "history",
	("GET", "/v1/me"): "identity",
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
	("POST", "/v1/tokens"): (
		"administrative",
		"`#208`. The capability reaches the CLI and HTTP; what it does not reach is the "
		"*client protocol*, which is what this file measures. `subroutine token create` opens "
		"the database directly, as §12.4 requires of the administrative commands — the first "
		"credential on an instance has to be mintable before there is a credential to "
		"authenticate a client with, so routing it through one would be a bootstrap that "
		"cannot complete. Same shape as `/v1/admin/backups`.",
	),
	("GET", "/v1/tokens"): (
		"administrative",
		"The inventory beside the issuing, and `subroutine token list` reads it without a "
		"running service, which is the whole of §12.4.",
	),
	("DELETE", "/v1/tokens/{id_or_prefix}"): (
		"administrative",
		"Revoking, and the one of the three that most has to work when the service is the "
		"thing that has gone wrong (§12.4): `subroutine token revoke` is the answer to a leak, "
		"and a client method for it would need the credential you are trying to burn.",
	),
	("GET", "/healthz"): (
		"protocol",
		"A liveness probe for whatever is in front of this. Not a capability anybody is "
		"being denied, and §10.4's `subroutine doctor` is the question a person asks.",
	),
	("GET", "/readyz"): (
		"protocol",
		"The same, plus the schema check §12.4a wants before a client trusts an instance. "
		"A client that could reach it would be asking whether the thing it is already "
		"talking to is up; `#89` is that question asked properly, by the CLI, before it "
		"connects at all.",
	),
	("GET", "/v1/docs/agent"): (
		"protocol",
		"§13.3's guide, written for an agent arriving with a base URL and a token and no "
		"other source. Somebody holding a client has already got past the problem it solves.",
	),
	("GET", "/v1/docs/examples"): (
		"protocol",
		"§13.3's worked calls, executed by `tests/test_api_examples.py`. They are HTTP by "
		"construction — that is what they are examples of, and a client wrapping them "
		"would be documentation nobody could run.",
	),
	("GET", "/v1/meta"): (
		"protocol",
		"What this build can do, in machine-readable form, for a client discovering an "
		"instance over HTTP. §12.2a's `subroutine help` is the same question asked by a "
		"person, and `--help` lists the commands.",
	),
	("GET", "/v1/projects/{id_or_key}"): (
		"tracked",
		"One project on its own. `project list` prints the tree and `show` reads items; "
		"nothing yet asks for a single project's own record. `#141`.",
	),
	("GET", "/v1/projects/{id_or_key}/events"): (
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
		"Only the author may edit a comment, and the honest alternative to editing attributed "
		"prose is deleting it. Low value from a CLI; nobody has asked. `#141`.",
	),
	("DELETE", "/v1/comments/{comment_id}"): (
		"tracked",
		"`#141`, alongside editing one — and the more useful of the pair, since deleting is "
		"the honest alternative to rewriting somebody's attributed words.",
	),
	("DELETE", "/v1/projects/{id_or_key}"): (
		"disclosure",
		"Deleting a project takes its tasks out of the visible world with it. That wants "
		"confirmation and a considered message rather than the same one-liner as `#140`. "
		"`#141`.",
	),
	("POST", "/v1/projects/{id_or_key}/restore"): (
		"disclosure",
		"The other half of the entry above, and it goes with it. `#308` built the endpoint "
		"because `DELETE` promised a reversal nothing provided; a client verb for putting a "
		"project back, where taking it away is still HTTP-only, would be the pair reachable "
		"from opposite directions. `#141` closes both.",
	),
}

#: Client methods the CLI does not call, and why.
NOT_IN_CLI: dict[str, Excuse] = {
	"close": (
		"protocol",
		"Resource lifetime, not a capability §13.7 could deny anybody. `cli/personal.py` "
		"closes its clients through the context manager `opened()` wraps, which is the same "
		"call by another spelling.",
	),
}

#: Client methods the MCP adapter does not call, and why. **The list `#149` is deleting.**
NOT_IN_MCP: dict[str, Excuse] = {
	"update_document": (
		"tracked",
		"`#293`, and this guard is what found it — the CLI half landed as `#291` and the "
		"agent half is a budget decision rather than an oversight. `subroutine_document` "
		"writes one and nothing revises it, so a session that concluded something wrongly "
		"cannot correct what the next session will read. Whether that is a new tool, a `ref` "
		"argument on the existing one, or nothing, is measured in `tests/test_mcp.py` the way "
		"the three previous raises were. **Deleting this entry is what closes `#293`.**",
	),
	"create_workspace": (
		"disclosure",
		"`#300`. A tenancy boundary, and an instance-tier permission no role can carry — only "
		"a superuser holds `instance:workspace_create` (§7.1). An agent that made one would "
		"be creating a place its own tools then could not see into, since a session reaches "
		"one connection and one workspace at a time (`#276`). §1.4's argument runs the other "
		"way here as it does for `move_project`: harder to reach is the feature.",
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
		"Who this credential is. §21.3's server instructions already name the connection "
		"an agent is on, so a tool for this would spend schema restating a fact every "
		"session already carries.",
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

	commands = {
		match.group(1)
		for match in re.finditer(r"\bsubroutine ([a-z][a-z-]*)", skill)
		if match.group(1) in registered
	}

	# The floor matters as much as the ceiling here: an extraction that reaches nothing passes
	# a ceiling test silently, which is the failure this comment exists because of.
	assert commands, "found no CLI commands in the skill — has this stopped reaching them?"

	assert len(commands) <= 3, (
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
