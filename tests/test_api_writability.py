"""Every field the API *reports* must be one the API can *set*, or say why not.

This guard exists because of a defect found on 2026-07-30 while trying to generate a
roadmap from the instance: ``estimate_minutes`` was rendered by the task view, printed by
``subroutine show``, drawn on the compact line and published in ``/v1/meta`` as
``max_estimate_minutes`` — and could be given a value only by the ``~4h`` token of a
quick-capture line at creation. ``POST /v1/tasks`` and ``PATCH /v1/tasks/{ref}`` both
refused ``estimate`` and ``estimate_minutes`` with a 422. So an estimate could be supplied
only by whoever typed the original sentence, and could never be revised.

That is this codebase's recurring defect wearing its newest disguise. The family is *a rule
documented, believed, and implemented by nothing*; the variant is **a field that is readable
and unsettable**, which looks complete from every direction except the one that writes it.
§6.3's ``urgency`` was the same shape — a column, a constraint, a sort key and a compact-line
cell, with no way to set it — and so was ``visible_status_keys``.

Reading cannot catch it, because nothing is *wrong* at any single site. The view is right,
the service is right, the endpoint is right about the fields it does declare. Only the
comparison between them shows the hole, which is why it is made here and made mechanically.

**How to satisfy this test when it fails.** It has found a field the API reports and cannot
set. Either add it to the request models and the service — which is usually right — or
record it below with a reason:

* :data:`DERIVED` — the system computes it; a client supplying one would be describing
  rather than deciding. Ids, timestamps, ``priority_score``.
* :data:`AT_CREATION` — settable when the item is made and deliberately fixed afterwards.
  A short list, and every entry needs an argument stronger than "we did not build it".
* :data:`UNSETTABLE` — a genuine gap, with the ref of the item tracking it. **Deleting an
  entry here is what closes that item**, and an entry with no tracking ref fails.

The second direction is checked too: a name recorded here that the view no longer has is a
failure, so this file cannot quietly describe a model that has moved on.
"""

import re
import typing
import uuid

import pydantic
import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.api.documents
import subroutine.api.tasks
import subroutine.db.base
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.system
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.views

#: View field -> the request field that writes it, where the two are spelled differently.
#: ``due``/``start`` take a date *expression* and the view reports the instant it resolved
#: to; ``estimate`` takes §6.4's grammar and the view reports minutes. A name absent here is
#: expected to be spelled the same on both sides.
WRITTEN_AS: dict[str, str] = {
	"due_at": "due",
	"snoozed_until": "snooze",
	"starts_at": "starts",
	"recurrence_rule": "recurrence",
	"estimate_minutes": "estimate",
	"project_id": "project",
	"body": "body",

	#: §8.5 reports a relation as an id and `#493` accepts it as a **name** — so this is the
	#: one entry here where the two spellings differ in *kind* rather than in wording. A caller
	#: holding a UUID for a person is a caller who has already made a request they should not
	#: have had to; a reader wanting the person's details has the id and one call. Both are
	#: right, which is why the mismatch is recorded rather than resolved.
	"assignee_id": "assignee",

	#: Both written by ``POST /…/move``'s ``parent`` (`#44`), which takes a ref or an id where
	#: the view reports an id — the same trade `assignee_id` records, for the same reason. The
	#: two views spell the *column* differently and the endpoint does not, because a document's
	#: parent is another document and a task's is another task: there is nothing to
	#: disambiguate once you are at the endpoint for one of them.
	"parent_task_id": "parent",
	"parent_id": "parent",
}

#: Computed, allocated or maintained by the system. A client cannot supply these because a
#: client does not decide them.
DERIVED: dict[str, str] = {
	#: **The workings of a repeat, and none of them a caller's to send** (`#94`). `#915`
	#: settles what each is: the words somebody typed, which occurrence this is, which
	#: template it came from, and whether this row is the rule rather than the work.
	"recurrence_text": (
		"the phrase as written, kept so a reader sees their own words back rather than "
		"FREQ=WEEKLY;INTERVAL=2;BYDAY=TU. Null when a rule was sent directly."
	),
	"occurrence_at": "which occurrence of the series this is, computed from the rule.",
	"recurrence_template_ref": "the template this came from; set when it is materialised.",
	"is_template": (
		"whether this row is the rule rather than the work. Follows from having been "
		"created with a recurrence, and is what explains why a row with a ref is in no "
		"listing."
	),
	"id": "Allocated by the service.",
	"assigned_by_id": (
		"Who assigned it, taken from whoever made the change (`#477`). A caller supplying one "
		"would be making a claim about an act rather than recording it."
	),
	"ref": "Allocated once per workspace and never reused (§6.2).",
	"relevance": (
		"How well this row answered the search that selected it (`#823`). Published on Simon's "
		"decision of 2026-08-14 so that a search returns the same order at the terminal, over "
		"HTTP, through MCP and in the browser — the last of which holds tasks and documents as "
		"two collections and cannot interleave them into one ranked list without a shared key "
		"(`#875`). Computed by the query and never accepted: a caller supplying one would be "
		"claiming a score against words it chose itself."
	),
	"rank": (
		"Where the ordering the listing asked for put this row (`#569`). Not a score anybody "
		"assessed — `priority_score` is that — and not stored anywhere: the query computes it "
		"and it arrives on the row, so there is nothing for a caller to set. Accepting one "
		"would let a client claim a position in an order it did not run."
	),
	"size_bytes": (
		"How many bytes of prose the item carries, measured from the description or the body "
		"as it goes over the wire (`#595`). A caller supplying one would be describing the "
		"thing it just sent rather than deciding anything, and a value that disagreed with the "
		"text beside it would be worse than none — the whole point is that a reader can trust "
		"it before spending a context window on the field it measures."
	),
	"project_key": "The key of project_id, resolved for display.",
	"project_path": "The whole address of project_id — its ancestors' keys and its own, composed for display (`#512`). Derived from the tree rather than stored, so moving a project changes it without anybody writing it.",
	"claimed_by": (
		"The username of claimed_by_id, resolved for display (`#726`) — the same shape as "
		"`assignee` beside `assignee_id`. A lease is taken and given back by `claim` and "
		"`release`, which decide the holder from the credential presenting them: naming "
		"somebody else in a body would be claiming a lease on their behalf, which is the one "
		"thing a lease must not allow."
	),
	"status_category": "The fixed category of the status (§5.5); an installation names statuses, not categories.",
	"status_id": "The id of the status named by `status`.",
	"status_is_default": (
		"Whether `status` is the one items start in — a property of the workspace's "
		"vocabulary, not of this item. Reported so a surface can tell a status somebody "
		"chose from the absence of a choice (`#168`), which is what let `subroutine show` "
		"print `blocked` while staying quiet about `open`."
	),
	"type_id": "The id of the type named by `type`.",
	"priority_score": "importance x urgency (§6.3). Derived, and null unless both are set.",
	"blocked": (
		"Whether an unfinished task blocks this one, read off the `blocks` links rather than "
		"stored (`#425`). Writing it would mean two answers to one question — the links and a "
		"flag — and the flag would be the one that went stale, since it changes when *another* "
		"item completes. Make it true or false by linking or by finishing the blocker."
	),
	"blocking": (
		"Whether this task holds an unfinished one up — the mirror of `blocked` above, and "
		"read off the same `blocks` links rather than stored (`#569`). Writing it has the same "
		"defect from the other end: two answers to one question, and the flag would be the one "
		"that went stale, because it changes when *another* item completes. Make it true or "
		"false by linking or by finishing what it holds up."
	),
	"estimate_human": "estimate_minutes as a person would say it (§6.4). Written by sending `estimate`.",
	# **`estimate_human`'s twin, and filed under the same argument** (`SR#925`). §6.7 requires a
	# repeat to be read back in *different words* from the ones typed — that is what makes it a
	# check rather than a mirror — so this is generated from `recurrence_rule` and its anchor
	# and can never be sent. Sending it would be sending the confirmation instead of the thing
	# being confirmed.
	"recurrence_description": (
		"recurrence_rule as a sentence, generated rather than echoed (§6.7). Written by "
		"sending `recurrence`."
	),
	"completed_at": "Follows the status category (§10.7 invariant 5), never set directly.",
	"archived_at": "Archiving is its own operation, not a field to assign a timestamp to.",
	"deleted_at": "Deletion is its own operation; DELETE sets it.",
	"created_at": "When the row was written.",
	"updated_at": "When the row last changed.",
	"content_updated_at": "When the *meaning* last changed (§6.1); the service decides, not the caller.",
	"version": "The concurrency token (§8.9). Sent back as `expected_version`, never assigned.",
	"supersedes_id": "Set by writing the superseding document, not by editing the superseded one.",
	"parent_ref": (
		"The ref of parent_task_id, resolved for display — the same relationship project_key "
		"has to project_id. Re-parenting is written by setting the parent, which is #44."
	),
	"parent_title": (
		"The title of parent_task_id, resolved so a client can render a subtree without a "
		"call per row. Changed by editing the parent, never through the child."
	),
	"claimed_by_id": (
		"Who holds a lease on this task (§14.11). Claiming is its own operation, like "
		"completing and deleting — assigning a holder by hand would let a caller park work "
		"under somebody else's name, which is the one thing a lease exists to make honest."
	),
	"claimed_at": "When the current lease was first taken. Follows the claim, never assigned.",
	"claim_expires_at": (
		"When the lease runs out. Set from the lease length at claim time, because a caller "
		"choosing its own expiry could take a lock and call it a lease."
	),
	"created_by": "The actor who wrote the row. Taken from the credential, never from the body — a caller that could name someone else could forge attribution.",
	"updated_by": "The actor who last changed it, on the same terms.",
}

#: Settable at creation and fixed afterwards, on purpose.
AT_CREATION: dict[str, str] = {
	"workspace_id": (
		"An item's workspace is the middle segment of every `work/acme/#42` it has been "
		"written as (§13.7) and the tenancy of its ref (§6.2). Moving one is a decision "
		"before it is a feature - see #30."
	),
}

#: Reported, not settable, and that is a defect rather than a decision. **Every entry names
#: the item tracking it, and removing the entry is what closes that item.** All four were
#: found by this test the first time it ran.
#: **Empty, and that is a state rather than a mistake.** Both entries it held were `#44` —
#: a task could not be re-parented and a document could not be nested at all — and both were
#: closed by ``POST /…/move`` on 2026-08-15. The checks below still fire the moment anything
#: is added; an empty register means every field these views report can now be written.
UNSETTABLE: dict[str, str] = {}


def _fields (model: type[pydantic.BaseModel]) -> frozenset[str]:
	"""Return the field names a pydantic model declares."""

	return frozenset(model.model_fields)


#: The three views that have request models behind them, with the models that write them.
#: ``(view, create, update)``.
#: One view, the model that creates it, and **every** model that can change it afterwards.
#:
#: **That last one was a single model until `#44`**, and the narrowing was invisible because
#: nothing had ever been changed by anything but ``PATCH``. §8 reserves an explicit verb
#: sub-resource where an operation is genuinely not CRUD, and re-parenting is one: it can be
#: refused for being a *cycle*, which is a question about the shape of the tree rather than
#: about this row. So a field became settable by an endpoint this guard could not see, and the
#: entry calling it unsettable would have gone on reading as an open defect.
SURFACES: tuple[
	tuple[
		str,
		type[pydantic.BaseModel],
		type[pydantic.BaseModel],
		tuple[type[pydantic.BaseModel], ...],
	],
	...,
] = (
	(
		"task",
		subroutine.views.Task,
		subroutine.api.tasks.Create,
		(subroutine.api.tasks.Update, subroutine.api.tasks.Move),
	),
	(
		"document",
		subroutine.views.Document,
		subroutine.api.documents.Create,
		(subroutine.api.documents.Update, subroutine.api.documents.Move),
	),
)


def _changeable (models: tuple[type[pydantic.BaseModel], ...]) -> frozenset[str]:
	"""Return every field name some endpoint can change after creation."""

	return frozenset().union(*(_fields(model) for model in models))


@pytest.mark.parametrize(
	("name", "view", "create", "changing"), SURFACES, ids=[row[0] for row in SURFACES]
)
def test_every_reported_field_can_be_written_or_says_why_not (
	name: str,
	view: type[pydantic.BaseModel],
	create: type[pydantic.BaseModel],
	changing: tuple[type[pydantic.BaseModel], ...],
) -> None:
	"""Nothing this view reports may be unwritable without an entry above.

	The failure this was built from: ``estimate_minutes`` was reported by three surfaces and
	accepted by no endpoint, for as long as the field had existed.
	"""

	writable = _fields(create) | _changeable(changing)
	excused = set(DERIVED) | set(AT_CREATION) | set(UNSETTABLE)

	unexplained = sorted(
		field
		for field in _fields(view)
		if field not in excused and WRITTEN_AS.get(field, field) not in writable
	)

	assert not unexplained, (
		f"The {name} view reports {unexplained} and no endpoint accepts them. Add them to "
		f"the request models and the service, or record them in DERIVED, AT_CREATION or "
		f"UNSETTABLE in this file with a reason."
	)


def test_every_field_excused_here_is_still_a_field () -> None:
	"""The other direction, so this file cannot describe a model that has moved on.

	A stale exemption is worse than none: it reads as a considered decision about something
	that no longer exists, and it silently excuses whatever later takes the name.
	"""

	reported = _fields(subroutine.views.Task) | _fields(subroutine.views.Document)

	for register, label in (
		(DERIVED, "DERIVED"),
		(AT_CREATION, "AT_CREATION"),
		(UNSETTABLE, "UNSETTABLE"),
		(WRITTEN_AS, "WRITTEN_AS"),
	):
		unknown = sorted(field for field in register if field not in reported)

		assert not unknown, f"{label} names {unknown}, which no view reports any more."


@pytest.mark.parametrize(
	("name", "view", "create", "changing"), SURFACES, ids=[row[0] for row in SURFACES]
)
def test_nothing_recorded_as_a_gap_has_quietly_been_closed (
	name: str,
	view: type[pydantic.BaseModel],
	create: type[pydantic.BaseModel],
	changing: tuple[type[pydantic.BaseModel], ...],
) -> None:
	"""**The direction this file was missing**, found while closing ``#42``.

	The staleness check above asks whether an excused field still *exists*. Nothing asked
	whether it is still *unsettable* — so putting ``type`` on both update models closed the
	item and left the entry sitting there, reading as an open defect. An exemption that
	cannot go stale is one nobody deletes, and a register full of closed gaps is one nobody
	believes.

	This is the third time today the same lesson has arrived: an allow-list needs a check in
	the direction that says an entry is no longer needed, not only one that says it still
	parses.
	"""

	# **Against what can change it, never against create.** An entry here means the field
	# cannot be *changed* — `#44` was about re-parenting a task that exists, and
	# `parent_task_id` is accepted on create, so unioning the two would have reported it
	# closed while the gap it named was wide open. The first run of this check did exactly
	# that.
	#
	# **And "what can change it" is a set, not `Update`.** `#44` closed by adding a `move`
	# endpoint rather than a `PATCH` field, which this comparison could not see at all — so
	# the entry below would have gone on describing a gap that had been filled.
	changeable = _changeable(changing)

	closed = sorted(
		field
		for field in UNSETTABLE
		if field in _fields(view) and WRITTEN_AS.get(field, field) in changeable
	)

	assert not closed, (
		f"{closed} are recorded in UNSETTABLE and the {name} endpoints now accept them. "
		f"Delete the entries — that is what closes the items they name."
	)


def test_every_known_gap_names_the_item_tracking_it () -> None:
	"""A gap recorded without a ref is a gap nobody is going to close.

	``UNSETTABLE`` is the only register that describes a defect rather than a decision, so
	it is the only one that can rot into a permanent excuse. Requiring a ref makes deleting
	an entry the thing that closes an item, rather than a tidy-up nobody does.
	"""

	for field, reason in UNSETTABLE.items():
		assert "#" in reason, f"{field!r} is recorded as a gap with no item tracking it."


#: Columns that are machinery rather than facts about the item. A client cannot act on them
#: and reporting them would be reporting how this is stored rather than what it holds.
INTERNAL: dict[str, str] = {
	"parked": (
		"Which band the deferral ordering put this row in (`#877`) — nought for work that can "
		"be started, one for work somebody has put off. Not stored and not reported, and the "
		"second half is a decision rather than an oversight: the band is a fact about the "
		"clock at the moment the query ran, so a published boolean would go on saying "
		"*deferred* on a page a reader leaves open past the instant. Both clients recompute "
		"it from `snoozed_until`, which the row already carries because the mark says *when*, and "
		"`ordering.put_off` is the one spelling of that rule. `rank`'s sibling in mechanism "
		"and its opposite in this."
	),
	"path": "The materialised path (§6.9). An implementation of the hierarchy, not a field of it.",
	"depth": "Derived from `path`, and maintained with it.",
	"meta": "The extension bag (§6.14). Unexposed until something writes to it.",
	# **Everything below arrived when `#443` widened the walk past `Task` and `Document`.**
	# Each is machinery, a secret, or a normalisation — and the qualified spellings are the
	# ones that must not be excused on any other model.
	"workspace_id": (
		"The tenant key. Every view is already scoped to one workspace and a client that "
		"reached a row has named it, so reporting it is reporting how this is partitioned."
	),
	"entity_type": (
		"Which kind of item a vocabulary or a link end belongs to. Reported *as structure* "
		"rather than as a field — `/v1/meta` keys `statuses` and `item_types` by it, and a "
		"link end carries it — so publishing it again would be the same fact twice."
	),
	"name_normalized": "The case-folded form a unique index compares. Never the name.",
	"username_normalized": "The same, for §7.1's uniqueness.",
	"email_normalized": "The same.",
	"ApiToken.token_hash": "The credential. §7.4: stored as a hash and reportable by nothing.",
	"ApiToken.token_prefix": (
		"The lookup half of a credential. `views.Token` reports it as `prefix`; the column "
		"name differs and the fact does not."
	),
	"User.password_hash": "§7.4 again. A hash is not a field, it is the absence of one.",
	"Instance.singleton": (
		"The constant column a unique index uses to make this table hold one row. An "
		"implementation of *there is one instance*, which the response already says by shape."
	),
	"Workspace.next_ref_number": (
		"The ref counter (§6.2). `views.py` already argues its absence: a client that read it "
		"would be reading a number that is only true until the next write."
	),
	"Comment.parent_comment_id": (
		"§5.10's escape hatch, deliberately unexposed — comments are flat and chronological "
		"by decision, and `domain/comments.py` carries the argument."
	),
	"Link.link_type_id": "Reported as `label`, which is the name §5.7 says a client uses.",
	"Link.source_id": "Reported as the `LinkEnd` pair, which carries the ref a caller addresses.",
	"Link.source_type": "The same end.",
	"Link.target_id": "The same, as `other`.",
	"Link.target_type": "The same end.",
	"ProjectMember.user_id": "Reported as the member themselves — `Member.user`.",
	"ProjectMember.role_id": "Reported as `Member.role`, the name rather than the id (§7.2).",
	"WorkspaceMember.user_id": "The same view, the same reason.",
	"WorkspaceMember.role_id": "The same.",
	"User.email": (
		"§7.1 makes an address a way to reach somebody rather than a fact about them, and "
		"nothing in the product mails anybody yet. Publishing every member's address on a "
		"listing any member can read is a decision nobody has taken."
	),
}

#: Columns that exist ahead of the feature that will use them. **Each names the milestone**,
#: because a column with no feature and no date is indistinguishable from one that was
#: forgotten — which is how `spent_minutes` and `urgency` both survived.
UNBUILT: dict[str, str] = {
	"is_template": "Recurrence (M7). The template flag has no feature to belong to yet.",
	"occurrence_at": "Recurrence (M7).",
	"recurrence_rule": "Recurrence (M7).",
	"recurrence_text": "Recurrence (M7).",
	"recurrence_anchor": "Recurrence (M7).",
	"recurrence_template_id": "Recurrence (M7).",
	"position": "#28 — manual backlog order is specified and nothing exposes it.",
}

#: Stored and never reported, and that is a defect rather than a decision. Same rule as
#: :data:`UNSETTABLE`: every entry names the item tracking it, and deleting the entry is what
#: closes that item.
UNREPORTED: dict[str, str] = {
	# **Found by `#854` renaming the *task*'s date columns**, which left this one carrying a
	# name that now means something else next door. Qualified rather than bare, because
	# `start_at` no longer exists on `Task` at all and an unqualified entry would read as
	# covering both.
	"Project.start_at": (
		"#917 — a project's own start and deadline are mapped, accepted by no request model, "
		"reported by no view and referenced nowhere in `src/`. The item carries the "
		"measurement and the argument for deleting rather than wiring."
	),
	"Project.due_at": "#917 — the same pair; see above.",
	"spent_minutes": (
		"#55 — §6.4 names it beside estimate_minutes and nothing reads or writes it."
	),
	# **The four `#443` found on its first widened run**, which is what that item was for: it
	# fixes nothing and finds the rest by itself. Every one was measured by grepping `src/`
	# rather than inferred from the schema — `#427`'s lesson, whose hand-written exclusion list
	# manufactured a gap that did not exist.
	"colour": (
		"#523 — stored on item_type, status and tag; written only by `db/seed.py` and read "
		"nowhere in `src/`. Decision `#102` is the argument for dropping it rather than "
		"reporting it, and `#441` is the reason to decide with the visual language rather "
		"than before it."
	),
	"Tag.description": "#523 — the same item: written by nothing, read by nothing.",
	"is_system": (
		"#524 — written by `db/seed.py` in three places and read nowhere. Unlike the colours "
		"this one is a candidate for *publishing*: `#445` records that `item_types` has no "
		"stable field a client can branch on, which is the gap this column would close."
	),
	"Project.timezone": (
		"#525 — §6.5's chain is explicit → user → workspace → instance and `schedule.zone_for` "
		"implements exactly that. A project is not a step in it, and nothing writes this."
	),
	"User.last_login_at": (
		"#526 — the string does not appear in `src/` outside `db/models/`, so it is null on "
		"every account everywhere."
	),
}


def _columns (model: type[typing.Any]) -> frozenset[str]:
	"""Return the column names a mapped model stores."""

	return frozenset(
		attribute.key for attribute in sqlalchemy.inspect(model).mapper.column_attrs
	)


#: Models whose view is not named after them, so the pairing cannot be derived from the name.
VIEWED_AS: dict[str, str] = {
	"ItemType": "Named",
	"ApiToken": "Token",
	"ProjectMember": "Member",
	"WorkspaceMember": "Member",
}

#: Mapped models that no view reports, and why that is right. **Each has to say what a reader
#: gets instead**, because "there is no view" is a description of the code rather than a reason.
NOT_VIEWED: dict[str, str] = {
	"TaskTag": (
		"An association row. Its content reaches a client as `Task.tags`, batch-loaded by "
		"`views.Vocabulary`, which is the whole of what it holds."
	),
	"DocumentTag": "The same, as `Document.tags`.",
	"Mention": (
		"The backlink index (§6.15). Reported through `?include=backlinks` on the item "
		"mentioned rather than as rows — a mention has no identity anybody addresses. That "
		"nothing *surfaces* them in the CLI is `#144`, which is about a listing rather than "
		"about this table being unreported."
	),
	"Role": (
		"A role reaches a client as its name — `Member.role`, `WorkspaceAccess.role` — because "
		"§7.2 makes the name the thing you grant and the permission set an implementation of "
		"it. A view would publish `permissions` as data somebody could believe was editable."
	),
	"LoginLink": (
		"A credential, and one that exists for minutes (`#248`). What reaches a caller is "
		"`views.SignInLink`, which carries the URL and nothing else — a view named after the "
		"row would have to report `token_hash` as excused, on a table whose whole content is "
		"a secret nobody may read back."
	),
	"WebSession": (
		"The same, and reported where it matters: `views.credential` describes the session "
		"the *caller* presented, as `kind='web_session'`, which is the only one anybody has a "
		"right to see. Somebody else's sessions are counted by `views.SignedOut` and never "
		"listed — an inventory of a person's live browsers is a map of where they work."
	),
}


def _mapped () -> dict[str, type[typing.Any]]:
	"""Return every model this application maps, by class name.

	**Read off the registry rather than listed** (`#443`). The version of this file that named
	two models could not report the eight it did not name, which is the shape `#405` went round
	the repository removing — an allow-list that is really the whole population.
	"""

	return {
		mapper.class_.__name__: mapper.class_
		for mapper in subroutine.db.base.Base.registry.mappers
	}


def _paired () -> list[tuple[str, type[typing.Any], type[pydantic.BaseModel]]]:
	"""Return each mapped model beside the view that ought to report it."""

	found = []

	for name, model in sorted(_mapped().items()):
		if name in NOT_VIEWED:
			continue

		view = getattr(subroutine.views, VIEWED_AS.get(name, name), None)

		if view is not None:
			found.append((name, model, view))

	return found


#: The stored side of the two surfaces above, paired with the view that ought to report it.
STORED = tuple(_paired())


def _excused (model: type[typing.Any], column: str, register: dict[str, str]) -> bool:
	"""Say whether one model's column is written off by one register.

	**Two spellings, and the qualified one is why widening this file was not a one-line
	change.** A bare column name excuses that name on *every* model, which was harmless while
	two models were walked and is not now: ``timezone`` is unread on `Project` (`#525`) and
	reported on three other models, so excusing it by name alone would blind the guard to the
	three that work in order to describe the one that does not. ``Project.timezone`` says the
	one. Bare names are kept for the columns that genuinely mean the same thing everywhere.
	"""

	return column in register or f"{model.__name__}.{column}" in register


@pytest.mark.parametrize(("name", "model", "view"), STORED, ids=[row[0] for row in STORED])
def test_every_stored_column_is_reported_or_says_why_not (
	name: str, model: type[typing.Any], view: type[pydantic.BaseModel]
) -> None:
	"""The other direction, and the one the first version of this file was blind to.

	The check above compares a *view* against its request models, so it catches "reported and
	unsettable". A column that never reaches the view is absent from that comparison
	entirely — so "stored and unreported" walked straight past it, and did: ``spent_minutes``
	was found by hand hours after the first guard went green, having been specified in §6.4
	and implemented by nothing since M1.

	Both directions are the same defect. A field the system holds and never mentions is as
	invisible as one it mentions and cannot set, and neither is wrong at any single site —
	only the comparison shows it.
	"""

	registers = (DERIVED, WRITTEN_AS, INTERNAL, UNBUILT, UNREPORTED)
	reported = set(view.model_fields)

	# A column may be reported under another name — `status_id` as `status`, `body` as
	# itself. `WRITTEN_AS` already records those pairings for the other direction.
	unexplained = sorted(
		column
		for column in _columns(model)
		if column not in reported
		and not any(_excused(model, column, register) for register in registers)
	)

	assert not unexplained, (
		f"The {name} table stores {unexplained} and no response reports them. Add them to "
		f"the view, or record them in INTERNAL, UNBUILT or UNREPORTED with a reason — "
		f"qualified as {name}.<column> unless the name means the same on every model."
	)


def test_every_mapped_model_is_walked_or_says_why_not () -> None:
	"""**The half that fails silently**, and the reason `#443` existed at all.

	This file walked ``Task`` and ``Document`` and reported nothing about the other eighteen —
	not as a decision, but because the list was written when there were two views and never
	revisited. A guard checking the shape it was written from, which is this repository's
	recorded signature and what `#405` went round removing.

	So the population comes off the mapper registry and a model leaves it only by being named
	in ``NOT_VIEWED`` with a reason. Adding a table now costs a line here or a view, and
	cannot cost nothing.
	"""

	walked = {name for name, _model, _view in STORED}
	unaccounted = sorted(set(_mapped()) - walked - set(NOT_VIEWED))

	assert not unaccounted, (
		f"{unaccounted} are mapped, have no view named after them and no entry in NOT_VIEWED, "
		f"so nothing checks what they store. Add a view, an alias in VIEWED_AS, or a reason."
	)


def test_the_walk_actually_reached_the_models () -> None:
	"""A floor, because a registry read that returned nothing would excuse everything.

	The check above passes vacuously against an empty walk — no models, nothing unaccounted —
	and so does every parametrised case, since "no cases failed" and "no cases ran" are the
	same output. `#405`'s lesson from ``test_cli_help``, which reported clean over one command
	out of forty-eight.
	"""

	assert len(_mapped()) >= 20, "the mapper registry read fewer models than this app defines"
	assert len(STORED) >= 15, "the pairing dropped models the registry has"

	names = {name for name, _model, _view in STORED}

	assert {"Task", "Document", "User", "Project", "Status"} <= names


def test_every_model_excused_from_the_walk_is_still_a_model () -> None:
	"""``NOT_VIEWED`` and ``VIEWED_AS`` rot the same way every allow-list here does."""

	mapped = set(_mapped())

	for register, label in ((NOT_VIEWED, "NOT_VIEWED"), (VIEWED_AS, "VIEWED_AS")):
		unknown = sorted(name for name in register if name not in mapped)

		assert not unknown, f"{label} names {unknown}, which this application no longer maps."

	for name, view in VIEWED_AS.items():
		assert hasattr(subroutine.views, view), (
			f"VIEWED_AS sends {name} to views.{view}, which does not exist."
		)


def test_every_column_excused_here_is_still_a_column () -> None:
	"""So this file cannot go on excusing something the schema has dropped.

	A stale exemption reads as a considered decision about a column that no longer exists,
	and silently excuses whatever later takes the name.

	**Both spellings are checked**, and the qualified one is the stricter: ``Project.timezone``
	is stale the moment either the model or the column goes, where a bare ``timezone`` survives
	as long as anything anywhere has one.
	"""

	mapped = _mapped()
	stored = {column for model in mapped.values() for column in _columns(model)}
	qualified = {
		f"{name}.{column}" for name, model in mapped.items() for column in _columns(model)
	}

	for register, label in (
		(INTERNAL, "INTERNAL"), (UNBUILT, "UNBUILT"), (UNREPORTED, "UNREPORTED")
	):
		unknown = sorted(
			entry
			for entry in register
			if entry not in stored and entry not in qualified
		)

		assert not unknown, f"{label} names {unknown}, which no table stores any more."


def test_every_unreported_column_names_the_item_tracking_it () -> None:
	"""``UNREPORTED`` is the only register here describing a defect, so it is the one that rots."""

	for column, reason in UNREPORTED.items():
		assert "#" in reason, f"{column!r} is recorded as a gap with no item tracking it."


#: A reason pointing at the place a reader gets the content instead — ``Task.tags``,
#: ``Member.role``. Capitalised on the left, because that is what tells a view apart from a
#: module (``views.Vocabulary``) or a setting; and both halves bare, so ``kind='web_session'``
#: and ``?include=backlinks`` are prose about a mechanism rather than a citation.
_CITED = re.compile(r"`([A-Z]\w*)\.(\w+)`")


def _citations (registers: dict[str, dict[str, str]]) -> list[tuple[str, str, str, str]]:
	"""Return every *"reaches a client as ``X.y``"* claim these registers make.

	Takes the registers rather than reading the module ones, so a synthetic entry naming a
	field nobody built can be put through the real scan — `#405`'s rule, and the only thing
	separating this from a check that reads nothing and approves everything.
	"""

	return [
		(label, entry, view, field)
		for label, register in registers.items()
		for entry, reason in register.items()
		for view, field in _CITED.findall(reason)
	]


#: Every register whose entries carry a written reason. All of them, deliberately: the six
#: citations that exist today sit in two, and a rule about how a reason is written should not
#: depend on which list somebody put the entry in.
EXCUSES: dict[str, dict[str, str]] = {
	"NOT_VIEWED": NOT_VIEWED,
	"DERIVED": DERIVED,
	"WRITTEN_AS": WRITTEN_AS,
	"INTERNAL": INTERNAL,
	"UNBUILT": UNBUILT,
	"UNREPORTED": UNREPORTED,
	"AT_CREATION": AT_CREATION,
	"UNSETTABLE": UNSETTABLE,
}


def test_an_excuse_naming_where_a_reader_gets_it_instead_is_checked_against_that_place () -> None:
	"""An excuse can describe something nobody built, and prose is compared to nothing.

	``NOT_VIEWED`` said of ``DocumentTag``: *"The same, as ``Document.tags``."* — and
	``views.Document`` had no ``tags`` field. The entry had read as a considered decision since
	`#443`, and it was a description of a destination that did not exist (`#820`). ``#819``
	later built the field, so the sentence is true now by accident of a feature landing three
	days afterwards, which is exactly the accident a guard should not depend on.

	**Every other stale-entry test in this repository asks whether an entry is still needed.
	This is the first that asks whether it was ever true.**

	Deliberately narrow. A reason may legitimately point at a mechanism (``?include=backlinks``)
	or at another item, so only the citation form is resolved and the rest of the prose is left
	alone — imposing a grammar on written reasons would cost more than it caught.
	"""

	found = _citations(EXCUSES)

	for label, entry, view, field in found:
		reported = getattr(subroutine.views, view, None)

		assert reported is not None and hasattr(reported, "model_fields"), (
			f"{label}[{entry!r}] says a reader gets this as {view}.{field}, and there is no "
			f"{view} view."
		)

		assert field in reported.model_fields, (
			f"{label}[{entry!r}] says a reader gets this as {view}.{field}, and {view} has no "
			f"{field}. The excuse names a place nobody built."
		)

	# A scan that matched nothing would approve every register in this file, which is the
	# failure this whole guard exists to describe.
	assert found, "No excuse cites where a reader gets the content — has this stopped reading?"


def test_the_citation_check_can_see_a_place_that_was_never_built () -> None:
	"""Fed the entry as it stood, through the real scan.

	Written because the check above passes today for two reasons that look identical from the
	outside: every citation resolves, *and* a regex that matched nothing would say the same.
	"""

	as_it_stood = {"NOT_VIEWED": {"DocumentTag": "The same, as `Document.nothing_like_this`."}}
	found = _citations(as_it_stood)

	assert found == [("NOT_VIEWED", "DocumentTag", "Document", "nothing_like_this")]
	assert "nothing_like_this" not in subroutine.views.Document.model_fields

	# And the forms that are prose rather than a citation stay out of it, which is what keeps
	# the rule from spreading to reasons that name a mechanism.
	assert not _citations(
		{"NOT_VIEWED": {"Mention": "Reported through `?include=backlinks`, by `views.Vocabulary`."}}
	)


def test_a_column_ahead_of_its_feature_names_the_milestone () -> None:
	"""A column with no feature and no date is indistinguishable from a forgotten one.

	That is not hypothetical: §6.3's ``urgency`` sat as a column with a constraint and no way
	to set it, and ``spent_minutes`` still does. Naming the milestone is what separates
	"waiting for M7" from "nobody noticed".
	"""

	for column, reason in UNBUILT.items():
		assert any(marker in reason for marker in ("M7", "#")), (
			f"{column!r} is excused as unbuilt without naming a milestone or an item."
		)


#: Nullable fields whose null is *deliberately* refused rather than accepted, and why. A 422
#: is a fine answer — the sweep below only forbids a **5xx**. An entry here says the refusal
#: was designed rather than discovered.
REFUSES_NULL: dict[str, str] = {
	"title": (
		"A title cannot be cleared — an item with no title is one nobody can find again, so "
		"both create and update hold it to the same standard (domain.text.require)."
	),
	"key": "A project key is its address (§5.2) and is fixed after creation.",
	# Changed, not cleared — `#295` made a rename possible and this list is about `null`.
	# A workspace with no short name has no middle segment for any address to carry (§13.7),
	# so it would be unreachable by name from the moment it was emptied.
	"slug": "A workspace's short name can be changed but not removed (§13.7, `#295`).",
	"status": "A project always has a status; clearing one has no meaning.",
}


def _nullable (model: type[pydantic.BaseModel]) -> list[str]:
	"""Return the fields this request model declares as accepting ``None``.

	Read off the annotation rather than listed by hand, so a field added to a request model
	is swept without anybody remembering to add it here — which is the same mechanism the two
	directions above use, and the reason they keep working.
	"""

	found = []

	for name, field in model.model_fields.items():
		annotation = field.annotation

		if annotation is None or type(None) in typing.get_args(annotation):
			found.append(name)

	return found


@pytest.fixture
def live (session: sqlalchemy.orm.Session) -> typing.Any:
	"""An installation reachable over HTTP, sharing the test's transaction.

	Built here rather than borrowed from ``test_api_tasks``: importing one test module from
	another gives mypy the same file under two module names, since ``tests/`` has no
	``__init__.py``. Eleven files now define a fixture of this shape, which is an argument for
	moving it into ``api_support`` — its own change, not this one.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Writability"
	)
	session.flush()

	application = api_support.build_app(api_support.factory_for(session))
	secret = issued.value.get_secret_value()

	class Live:
		"""Just enough of a client to PATCH one field at a time."""

		def call (self, method: str, path: str, **kwargs: typing.Any) -> typing.Any:
			"""Make an authenticated request."""

			return api_support.call(
				application, method, path, headers={"authorization": f"Bearer {secret}"}, **kwargs
			)

	return Live()


def test_a_null_on_any_nullable_field_is_never_a_server_error (live: typing.Any) -> None:
	"""**The third direction, and the one that was missing.**

	The two directions above ask whether a field can be *set* and whether it is *reported*.
	Neither asks what happens when a caller sends the null §8.3 tells them to send — and on
	2026-07-31 that turned out to be a **500 on tasks, documents and projects alike** for
	``{"title": null}``, plus a second on ``{"tags": null}`` shipped an hour earlier.

	Both were the same shape: a request model declares ``str | None`` in order to express
	"omitted", the router passes whatever arrived straight through, and the service was never
	written to expect ``None``. Nothing was wrong at any single site, which is why two reviews
	and both existing directions missed it.

	A 422 is a perfectly good answer and is what an unclearable field should give. What this
	forbids is a traceback, which is never an answer.
	"""

	made = {
		"/v1/tasks": live.call("POST", "/v1/tasks", json={"title": "Sweep me"}).json(),
		"/v1/documents": live.call("POST", "/v1/documents", json={"title": "Sweep me"}).json(),
	}
	targets: list[tuple[str, type[pydantic.BaseModel]]] = [
		(f"/v1/tasks/{made['/v1/tasks']['ref']}", subroutine.api.tasks.Update),
		(f"/v1/documents/{made['/v1/documents']['ref']}", subroutine.api.documents.Update),
	]

	crashed: list[str] = []

	for path, model in targets:
		for name in _nullable(model):
			response = live.call("PATCH", path, json={name: None})

			if response.status_code >= 500:
				crashed.append(f"{path} {{{name!r}: null}} -> {response.status_code}")

	assert not crashed, (
		"a null on a field the request model declares nullable produced a server error:\n"
		+ "\n".join(crashed)
		+ "\n§8.3 says null clears. Accept it, or refuse it with a 422 and record the field "
		"in REFUSES_NULL with the reason."
	)


def test_every_field_recorded_as_refusing_null_still_exists (live: typing.Any) -> None:
	"""The second half, as everywhere else here: an excuse for a field nobody has is debt.

	Without this, `REFUSES_NULL` becomes a list of names that used to matter — which is the
	failure the *reported and unsettable* direction already guards against, and the reason
	deleting an entry is what closes an item.
	"""

	declared = set()

	for model in (
		subroutine.api.tasks.Update,
		subroutine.api.tasks.Create,
		subroutine.api.documents.Update,
		subroutine.api.documents.Create,
	):
		declared |= set(model.model_fields)

	unknown = sorted(set(REFUSES_NULL) - declared - {"key", "slug"})

	assert not unknown, f"REFUSES_NULL names fields no request model has: {unknown}"
