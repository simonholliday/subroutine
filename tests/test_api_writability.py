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

import typing
import uuid

import pydantic
import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.api.documents
import subroutine.api.tasks
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
	"start_at": "start",
	"estimate_minutes": "estimate",
	"project_id": "project",
	"body": "body",
}

#: Computed, allocated or maintained by the system. A client cannot supply these because a
#: client does not decide them.
DERIVED: dict[str, str] = {
	"id": "Allocated by the service.",
	"assigned_by_id": (
		"Who assigned it, taken from whoever made the change (`#477`). A caller supplying one "
		"would be making a claim about an act rather than recording it."
	),
	"ref": "Allocated once per workspace and never reused (§6.2).",
	"project_key": "The key of project_id, resolved for display.",
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
	"estimate_human": "estimate_minutes as a person would say it (§6.4). Written by sending `estimate`.",
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
UNSETTABLE: dict[str, str] = {
	"parent_task_id": (
		"#44 - a subtask cannot be re-parented, or promoted to a top-level task, after it "
		"is created."
	),
	"parent_id": (
		"#44, and the worse half of it - a document's parent is accepted by no endpoint at "
		"all, on create or update. Nesting exists in the schema and in the response and "
		"cannot be reached from outside."
	),
}


def _fields (model: type[pydantic.BaseModel]) -> frozenset[str]:
	"""Return the field names a pydantic model declares."""

	return frozenset(model.model_fields)


#: The three views that have request models behind them, with the models that write them.
#: ``(view, create, update)``.
SURFACES: tuple[tuple[str, type[pydantic.BaseModel], type[pydantic.BaseModel], type[pydantic.BaseModel]], ...] = (
	(
		"task",
		subroutine.views.Task,
		subroutine.api.tasks.Create,
		subroutine.api.tasks.Update,
	),
	(
		"document",
		subroutine.views.Document,
		subroutine.api.documents.Create,
		subroutine.api.documents.Update,
	),
)


@pytest.mark.parametrize(
	("name", "view", "create", "update"), SURFACES, ids=[row[0] for row in SURFACES]
)
def test_every_reported_field_can_be_written_or_says_why_not (
	name: str,
	view: type[pydantic.BaseModel],
	create: type[pydantic.BaseModel],
	update: type[pydantic.BaseModel],
) -> None:
	"""Nothing this view reports may be unwritable without an entry above.

	The failure this was built from: ``estimate_minutes`` was reported by three surfaces and
	accepted by no endpoint, for as long as the field had existed.
	"""

	writable = _fields(create) | _fields(update)
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
	("name", "view", "create", "update"), SURFACES, ids=[row[0] for row in SURFACES]
)
def test_nothing_recorded_as_a_gap_has_quietly_been_closed (
	name: str,
	view: type[pydantic.BaseModel],
	create: type[pydantic.BaseModel],
	update: type[pydantic.BaseModel],
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

	# **Against the update model alone, not create.** An entry here means the field cannot be
	# *changed* — `#44` is about re-parenting a task that exists, and `parent_task_id` is
	# accepted on create, so unioning the two would report it closed while the gap it names is
	# wide open. The first run of this check did exactly that.
	closed = sorted(
		field
		for field in UNSETTABLE
		if field in _fields(view) and WRITTEN_AS.get(field, field) in _fields(update)
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
	"path": "The materialised path (§6.9). An implementation of the hierarchy, not a field of it.",
	"depth": "Derived from `path`, and maintained with it.",
	"meta": "The extension bag (§6.14). Unexposed until something writes to it.",
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
	"spent_minutes": (
		"#55 — §6.4 names it beside estimate_minutes and nothing reads or writes it."
	),
}


def _columns (model: type[typing.Any]) -> frozenset[str]:
	"""Return the column names a mapped model stores."""

	return frozenset(
		attribute.key for attribute in sqlalchemy.inspect(model).mapper.column_attrs
	)


#: The stored side of the two surfaces above, paired with the view that ought to report it.
STORED: tuple[tuple[str, type[typing.Any], type[pydantic.BaseModel]], ...] = (
	("task", subroutine.db.models.work.Task, subroutine.views.Task),
	("document", subroutine.db.models.work.Document, subroutine.views.Document),
)


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

	excused = set(DERIVED) | set(WRITTEN_AS) | set(INTERNAL) | set(UNBUILT) | set(UNREPORTED)
	reported = set(view.model_fields)

	# A column may be reported under another name — `status_id` as `status`, `body` as
	# itself. `WRITTEN_AS` already records those pairings for the other direction.
	unexplained = sorted(
		column
		for column in _columns(model)
		if column not in reported and column not in excused
	)

	assert not unexplained, (
		f"The {name} table stores {unexplained} and no response reports them. Add them to "
		f"the view, or record them in INTERNAL, UNBUILT or UNREPORTED with a reason."
	)


def test_every_column_excused_here_is_still_a_column () -> None:
	"""So this file cannot go on excusing something the schema has dropped.

	A stale exemption reads as a considered decision about a column that no longer exists,
	and silently excuses whatever later takes the name.
	"""

	stored = _columns(subroutine.db.models.work.Task) | _columns(
		subroutine.db.models.work.Document
	)

	for register, label in ((INTERNAL, "INTERNAL"), (UNBUILT, "UNBUILT"), (UNREPORTED, "UNREPORTED")):
		unknown = sorted(column for column in register if column not in stored)

		assert not unknown, f"{label} names {unknown}, which neither table stores any more."


def test_every_unreported_column_names_the_item_tracking_it () -> None:
	"""``UNREPORTED`` is the only register here describing a defect, so it is the one that rots."""

	for column, reason in UNREPORTED.items():
		assert "#" in reason, f"{column!r} is recorded as a gap with no item tracking it."


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
