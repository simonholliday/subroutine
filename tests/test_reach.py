"""What the HTTP API can do and the clients cannot — SPEC.md §13.7, item ``#139``.

**Three walls in one afternoon, all the same shape, all found by hand.** ``#134``: a project
could not be created outside HTTP, so on a default install — where nothing runs ``serve`` — the
only project anybody would ever have was the Inbox. ``#136``: readiness, the one question this
tool answers that a list of tasks does not, was a query parameter and nothing else. ``#138``: a
document could not be written, while the MCP adapter's own tool description told agents to
write one.

Each was found by trying to *write documentation* and discovering the sentence could not be
true. That is a slow and lucky way to find a structural gap, and there was no reason to think
three was all of them.

The cause is structural rather than careless. The HTTP API is where a capability lands first;
``clients/base.Client`` is a hand-maintained protocol somebody has to extend to match; and
nothing compared the two. A capability that never reaches the protocol is invisible to the CLI
and to MCP — the two surfaces a person and an agent actually touch — and invisible full stop on
an installation that never starts a server.

So: every mutating route is either **named against the method that reaches it**, or **listed as
unreached with a written reason**. Adding an endpoint fails this until somebody decides which,
and that decision is the whole value. It does not argue that everything should be reachable —
several things here should not be — only that skipping one becomes a decision somebody recorded
rather than an omission nobody noticed.
"""

import subroutine.api.app
import subroutine.api.routing
import subroutine.clients.base

#: The verbs that change something. A reader that no client can reach is a missing feature; a
#: *writer* that no client can reach is a capability the product has and does not offer.
MUTATING = frozenset({"POST", "PATCH", "PUT", "DELETE"})

#: Mutating routes, and the :class:`~subroutine.clients.base.Client` method that reaches each.
REACHED_BY: dict[tuple[str, str], str] = {
	("POST", "/v1/tasks"): "capture",
	("PATCH", "/v1/tasks/{id_or_ref}"): "update",
	("POST", "/v1/tasks/{id_or_ref}/complete"): "complete",
	("POST", "/v1/tasks/{id_or_ref}/comments"): "remark",
	("POST", "/v1/documents"): "create_document",
	("POST", "/v1/documents/{id_or_ref}/comments"): "remark",
	("POST", "/v1/projects"): "create_project",
	("POST", "/v1/projects/{id_or_key}/comments"): "remark",
	("DELETE", "/v1/tasks/{id_or_ref}"): "discard",
	("DELETE", "/v1/documents/{id_or_ref}"): "discard",
	("POST", "/v1/tasks/{id_or_ref}/restore"): "undiscard",
	("POST", "/v1/documents/{id_or_ref}/restore"): "undiscard",
}

#: Mutating routes no client reaches, and why. **A reason, not a shrug** — "not built yet" is
#: only an acceptable entry when somebody has decided it can wait, and writing it down is what
#: turns that into a decision. Deleting an entry is what closes it.
NOT_REACHED: dict[tuple[str, str], str] = {
	("POST", "/v1/admin/backups"): (
		"Administrative, and deliberately so. It needs `instance:admin`, which no role "
		"carries, and §12.4's recovery property wants `subroutine db backup` to work when "
		"the service will not start. A client method would be a second path to a thing the "
		"CLI already does better."
	),
	("POST", "/v1/workspaces"): (
		"`init` makes one and most installations need exactly one, so this is a real gap "
		"without being a wall — `#141`. Unlike a project, nothing refuses for want of it."
	),
	("PATCH", "/v1/workspaces/{id_or_slug}"): (
		"Same as creating one, and rarer: a workspace's title and timezone are set once. "
		"`#141`."
	),
	("PATCH", "/v1/projects/{id_or_key}"): (
		"Renaming a project or changing its visibility. `#141` — the key, which is the part "
		"that cannot be changed at all, is settled at creation and that is the part that "
		"matters."
	),
	("POST", "/v1/projects/{id_or_key}/move"): (
		"Reparenting a whole subtree. Rare, consequential, and there is no undo, so it is "
		"the last of these to want a one-line command. `#141`."
	),
	("PATCH", "/v1/documents/{id_or_ref}"): (
		"Editing a document's body. Waits on `#15` (`subroutine edit`), because the answer "
		"is an editor rather than a flag — a `--body` that replaces a specification from a "
		"shell argument is a worse offer than none."
	),
	("PATCH", "/v1/comments/{comment_id}"): (
		"Only the author may edit a comment, and the honest alternative to editing attributed "
		"prose is deleting it. Low value from a CLI; nobody has asked. `#141`."
	),
	("DELETE", "/v1/comments/{comment_id}"): (
		"`#141`, alongside editing one — and the more useful of the pair, since deleting is "
		"the honest alternative to rewriting somebody's attributed words."
	),
	("DELETE", "/v1/projects/{id_or_key}"): (
		"Deleting a project takes its tasks out of the visible world with it. That wants "
		"confirmation and a considered message rather than the same one-liner. `#141`."
	),
	("POST", "/v1/tasks/{id_or_ref}/links"): (
		"Joining two items — blocks, relates_to, derives_from. Reading them is reached "
		"(`links`) and `show` renders them; writing one is not. `#141`, and it is the highest "
		"of these, because `blocks` is what readiness reads."
	),
	("DELETE", "/v1/tasks/{id_or_ref}/links/{link_id}"): (
		"`#141`, alongside making one. A link somebody added by mistake blocks work that is "
		"not blocked, which readiness then hides — so this follows the writer closely."
	),
	("POST", "/v1/documents/{id_or_ref}/links"): (
		"`#141`, alongside the task links, and the same shape — `derives_from` is how a "
		"decision document and the work it produced find each other later."
	),
	("DELETE", "/v1/documents/{id_or_ref}/links/{link_id}"): (
		"`#141`, likewise, and for the reason the task version gives: an unwanted link is "
		"worse than a missing one, because it silently narrows what looks startable."
	),
}


def _mutating () -> set[tuple[str, str]]:
	"""Return every route that changes something, as (verb, path)."""

	found = set()

	for path, methods in subroutine.api.routing.declarations(subroutine.api.app.ROUTERS):
		for verb in methods & MUTATING:
			found.add((verb, path))

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


def test_neither_list_names_a_route_that_no_longer_exists () -> None:
	"""The other direction, and the one that lets an entry be deleted with confidence.

	Without it a stale exemption outlives the endpoint it excused, and the next reader has no
	way to tell a decision from a leftover.
	"""

	live = _mutating()
	stale = (set(REACHED_BY) | set(NOT_REACHED)) - live

	assert not stale, f"{sorted(stale)} are listed here and are not routes"


def test_every_method_named_here_exists_on_the_protocol () -> None:
	"""So a rename shows up here rather than in a caller.

	``Client`` is a ``typing.Protocol``, so its methods are ordinary attributes of the class
	object; nothing has to be instantiated to ask.
	"""

	declared = {
		name
		for name in dir(subroutine.clients.base.Client)
		if not name.startswith("_") and callable(getattr(subroutine.clients.base.Client, name))
	}
	named = set(REACHED_BY.values())

	assert named <= declared, f"{sorted(named - declared)} are not methods of Client"


def test_every_reason_is_a_reason () -> None:
	"""An exemption list is only worth having while the entries say something.

	The failure mode is a one-word "later" that reads as considered and is not, and it arrives
	the first time somebody is in a hurry — which is exactly when the endpoint they are adding
	deserves the thought.
	"""

	for route, reason in NOT_REACHED.items():
		assert len(reason) > 40, f"{route} is excused by {reason!r}, which explains nothing"
		assert "#1" in reason or "§" in reason, (
			f"{route} is excused without naming an item or a specification section, so nothing "
			f"tracks it and nothing says it was a decision"
		)

