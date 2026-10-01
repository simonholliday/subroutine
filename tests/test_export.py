"""Exporting: every kind of row a credential may take away, a page at a time - `#4052`.

Decision `#4049`. The route and the local client page through one function,
``subroutine.exporting.page``, so the two can disagree only in how they ask for the next page,
which is what the first tests hold. **Who may read what is ``domain/scoping.py``'s**, and the
tests below read as somebody outside a private project to show the export inherits it rather
than restating it.
"""

import datetime
import json
import typing

import pytest

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.saved
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.errors
import subroutine.exporting
import subroutine.views
import test_authorization
import test_transport_equivalence

#: The equivalence suite's pair - one installation, reached locally and over HTTP - bound here
#: so pytest finds it as this module's fixture too.
pair = test_transport_equivalence.pair
Pair = test_transport_equivalence.Pair


@pytest.fixture(autouse=True)
def _every_event_settled (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Report events the moment they are written, rather than a second later.

	The change feed holds back its last second (§5.11) so a write still committing cannot be
	skipped, and the event log here is that feed's own page, so it holds it back too. A test
	writes and reads within that second, so without this every event it made is too young.
	"""

	monkeypatch.setattr(subroutine.domain.events, "WATERMARK", datetime.timedelta(0))


def _seeded (pair: Pair) -> dict[str, subroutine.views.Task]:
	"""Fill the installation with one of every kind an export holds, and return its tasks."""

	local = pair.local
	local.create_project(key="web", title="Website rebuild")
	fix = local.capture(text="Fix the deploy script", project="web").task
	notes = local.capture(text="Write the release notes", project="web").task
	gone = local.capture(text="Order a new phone for reception").task
	done = local.capture(text="Book the dojo for Thursday").task

	local.complete(ref=done.ref)
	local.discard(ref=gone.ref)
	local.capture(text="Water the plants every monday")
	local.link(ref=fix.ref, link_type="blocks", target=notes.ref)
	local.remark(ref=fix.ref, body="Tried on staging first.")
	local.verify(ref=fix.ref, passed=True, summary="The deploy ran clean.")
	local.create_document(title="Why the deploy script moved", body="It moved.", type="decision")
	local.create_tag(name="deploy", description="Anything that ships the site.")
	local.save_view(title="Deploys", arrangement="list", q="#deploy")

	return {"fix": fix, "notes": notes, "gone": gone, "done": done}


def _read (
	pair: Pair,
	reader: subroutine.domain.authentication.Principal,
	kind: str,
	*,
	size: int = 1000,
) -> list[typing.Any]:
	"""Return every row of one kind a principal may export, walking the pages as a client does."""

	found: list[typing.Any] = []
	after = None

	# **Bounded**, so a page that never moves on fails rather than hanging the suite.
	for _ in range(1000):
		page = subroutine.exporting.page(
				pair.session,
			reader,
			workspace_id=pair.workspace.id,
			kind=kind,
			after=after,
			size=size,
		)
		found.extend(page.items)

		if not page.has_more:
			return found

		after = page.resume

	raise AssertionError(f"the pages of {kind} never ended")


def test_every_kind_has_its_view_named_once () -> None:
	"""The routes are made from ``exporting.KINDS`` and the HTTP client reads ``views.EXPORTED``.

	Two declarations because the client must not import the queries, so the test is what holds
	them to one answer.
	"""

	assert {
		name: kind.view for name, kind in subroutine.exporting.KINDS.items()
	} == subroutine.views.EXPORTED


def test_both_transports_export_the_same_rows_of_every_kind (pair: Pair) -> None:
	"""One function behind both, so a difference here is in the paging or the parsing."""

	_seeded(pair)

	for kind in subroutine.views.EXPORTED:
		here = [row.model_dump(mode="json") for row in pair.local.export(kind)]
		there = [row.model_dump(mode="json") for row in pair.remote.export(kind)]

		assert here == there, kind
		assert here, f"nothing was exported as {kind}, so this compared nothing"


def test_an_export_holds_the_trash_what_is_done_and_the_templates (pair: Pair) -> None:
	"""Every listing leaves these out by default, and an export of what you have may not."""

	made = _seeded(pair)
	tasks = {row.ref: row for row in pair.remote.export("tasks")}

	assert tasks[made["gone"].ref].deleted_at is not None
	assert tasks[made["done"].ref].completed_at is not None
	assert any(row.is_template for row in tasks.values()), "a repeat's template is a row too"


def test_a_link_is_named_by_both_its_ends_and_says_who_made_it (pair: Pair) -> None:
	"""A link has no item to be seen from in an export, so it is an edge.

	It holds what either end's listing says of it: the type, what the type is, and its maker.
	"""

	made = _seeded(pair)
	(edge,) = [
		row for row in pair.remote.export("links") if row.source.ref == made["fix"].ref
	]

	assert edge.target.ref == made["notes"].ref
	assert edge.link_type == "blocks" and edge.link_category is not None
	assert edge.created_by == pair.user.id and edge.created_at is not None


def test_what_a_private_project_holds_is_left_out_for_somebody_outside_it (pair: Pair) -> None:
	"""Read through the domain as the owner and as a colleague, so only the reader differs.

	The project, its task, a comment on it, a link reaching it from outside, a verification of
	it, what happened to it and who it is shared with - each is in the owner's export and none in
	the colleague's. Who belongs to the workspace is in both, as its members listing is.
	"""

	made = _seeded(pair)
	pair.local.create_project(key="ops", title="Operations", visibility="private")
	hidden = pair.local.capture(text="Fix the leak on the third floor", project="ops").task
	pair.local.link(ref=made["fix"].ref, link_type="relates_to", target=hidden.ref)
	pair.local.remark(ref=hidden.ref, body="The plumber comes on Tuesday.")
	pair.local.verify(ref=hidden.ref, passed=False)

	# **A visible link after the hidden one**, and links read a row at a time below, so a page
	# holding only the hidden link has more after it - which is where a walk resumed from what
	# was shown, rather than from what was fetched, would stop or go round.
	pair.local.link(ref=made["notes"].ref, link_type="relates_to", target=made["done"].ref)

	# After the writes, because a second account is the end of the local client's single one.
	colleague = test_authorization._member(pair.session, pair.workspace, "member")
	owner = subroutine.domain.authentication.Principal(user=pair.user)

	for reader, sees in ((owner, True), (colleague, False)):
		links = _read(pair, reader, "links", size=1)
		verified = {row.task_ref for row in _read(pair, reader, "verifications")}

		assert ("ops" in {row.key for row in _read(pair, reader, "projects")}) is sees
		assert (hidden.ref in {row.ref for row in _read(pair, reader, "tasks")}) is sees
		bodies = {row.body for row in _read(pair, reader, "comments")}

		assert ("The plumber comes on Tuesday." in bodies) is sees
		assert any(hidden.ref in (row.source.ref, row.target.ref) for row in links) is sees
		assert (hidden.ref in verified) is sees
		assert (hidden.ref in {row.item_ref for row in _read(pair, reader, "events")}) is sees
		shared = {row.project for row in _read(pair, reader, "project_members")}

		assert ("ops" in shared) is sees
		assert "web" in shared, "a public project's members are anybody's in the workspace to take"
		assert {row.user.id for row in _read(pair, reader, "members")} == {
			pair.user.id,
			colleague.user.id,
		}

		# And what is outside the project is the colleague's to take as well.
		assert made["fix"].ref in {row.ref for row in _read(pair, reader, "tasks")}
		assert any(row.source.ref == made["notes"].ref for row in links), "the last link"


def test_a_withdrawn_link_and_a_deleted_comment_are_left_out (pair: Pair) -> None:
	"""Neither can be put back, so neither is in the trash; each is an event, which is exported."""

	made = _seeded(pair)
	link = pair.local.link(ref=made["fix"].ref, link_type="relates_to", target=made["done"].ref)
	pair.local.unlink(ref=made["fix"].ref, link_id=str(link.id))
	said = pair.local.remark(ref=made["notes"].ref, body="The draft is in the shared folder.")
	pair.local.uncomment(ref=made["notes"].ref, comment_id=str(said.id))

	assert link.id not in {row.id for row in pair.remote.export("links")}
	assert said.id not in {row.id for row in pair.remote.export("comments")}


def test_a_page_at_a_time_walks_every_row_once_in_the_order_they_were_made (pair: Pair) -> None:
	"""One row a page, following the route's own cursor, against the whole read in one go."""

	_seeded(pair)
	every = [str(row.id) for row in pair.remote.export("tasks")]
	walked: list[str] = []
	cursor: str | None = None

	# **Bounded**, so a cursor that never moves fails here rather than hanging the suite.
	for _ in range(len(every) + 1):
		asked = {"limit": "1"} | ({"cursor": cursor} if cursor else {})
		answer = pair.remote.call_api(method="GET", path="/v1/export/tasks", query=asked)

		assert answer.status == 200, answer.text

		body = json.loads(answer.text)
		walked.extend(item["id"] for item in body["items"])
		cursor = body["page"]["next_cursor"]

		if not body["page"]["has_more"]:
			break

	assert walked == every == sorted(every), "every row once, oldest first"
	assert len(every) >= 5


def test_the_event_log_is_the_change_feed_s (pair: Pair) -> None:
	"""Narrowed and ordered as ``/v1/changes`` is, because it is that feed's own page."""

	_seeded(pair)
	exported = [row.seq for row in pair.remote.export("events")]

	assert exported == [row.seq for row in pair.remote.changes(limit=10_000)]
	assert exported == sorted(exported) and len(exported) >= 10


def test_a_credential_is_refused_what_its_own_listing_would_refuse_it (pair: Pair) -> None:
	"""Each kind refuses as its listing does, so an export is never a wider door to the same rows.

	Without ``task:read`` there are no tasks and no documents, which are read under it; without
	``comment:read`` there are no comments, as ``GET /v1/tasks/<ref>/comments`` refuses them;
	and with it, a comment on a task still goes only to somebody who may read the task. Without
	``workspace:read`` there are no members, as ``GET /v1/workspaces/<slug>/members`` refuses
	them, while who is shared into a project goes with ``project:read``.
	"""

	_seeded(pair)
	document = next(iter(pair.local.export("documents")))
	pair.local.remark(ref=document.ref, entity_type="document", body="Read before moving it.")
	owner = subroutine.domain.authentication.Principal(user=pair.user)
	without_comments = test_authorization._with_token(
		pair.session, owner, scopes=["task:read", "project:read"]
	)
	without_tasks = test_authorization._with_token(
		pair.session, owner, scopes=["project:read", "comment:read"]
	)

	assert len(_read(pair, owner, "comments")) >= 2

	with pytest.raises(subroutine.errors.SubroutineError):
		_read(pair, without_comments, "comments")

	assert _read(pair, without_comments, "tasks")

	with pytest.raises(subroutine.errors.SubroutineError):
		_read(pair, without_comments, "members")

	assert _read(pair, without_comments, "project_members")

	for kind in ("tasks", "documents"):
		with pytest.raises(subroutine.errors.SubroutineError):
			_read(pair, without_tasks, kind)

	assert _read(pair, without_tasks, "comments") == []
	assert _read(pair, without_tasks, "projects")


def test_a_kind_nobody_has_heard_of_is_refused_by_name (pair: Pair) -> None:
	"""On both transports, before anything is asked of the instance, naming the kinds there are."""

	for client in pair.both():
		with pytest.raises(subroutine.errors.ValidationError) as caught:
			list(client.export("everything"))

		assert "tasks" in str(caught.value.errors), caught.value


#: The table each kind's view reports, so a field can be told from a stored column.
KIND_TABLES: dict[str, typing.Any] = {
	"workspace": subroutine.db.models.identity.Workspace,
	"projects": subroutine.db.models.project.Project,
	"tasks": subroutine.db.models.work.Task,
	"documents": subroutine.db.models.work.Document,
	"comments": subroutine.db.models.activity.Comment,
	"links": subroutine.db.models.work.Link,
	"verifications": subroutine.db.models.work.Verification,
	"events": subroutine.db.models.activity.Event,
	"tags": subroutine.db.models.vocabulary.Tag,
	"statuses": subroutine.db.models.vocabulary.Status,
	"item_types": subroutine.db.models.vocabulary.ItemType,
	"link_types": subroutine.db.models.vocabulary.LinkType,
	"saved_views": subroutine.db.models.saved.SavedView,
	"users": subroutine.db.models.identity.User,
	"members": subroutine.db.models.identity.WorkspaceMember,
	"project_members": subroutine.db.models.project.ProjectMember,
}

#: **Fields that are no column of their row and are kept, each a name for something it stores**
#: (`#4053`, decision `#4049`): a key, a label, a username or a number beside the id it names.
#: The other side is ``exporting.COMPUTED``. A field on neither fails below, so a field added to
#: a view is placed before it can be exported.
NAMED: dict[str, frozenset[str]] = {
	"workspace": frozenset({"prioritised_project"}),
	"projects": frozenset({"status", "status_category", "status_is_default", "status_label"}),
	"tasks": frozenset({
		"workspace",
		"project_key",
		"project_path",
		"claimed_by",
		"claimed_by_is_agent",
		"parent_ref",
		"parent_title",
		"status",
		"status_category",
		"status_is_default",
		"status_label",
		"type",
		"type_label",
		"type_category",
		"type_is_default",
		"assignee",
		"assignee_is_agent",
		"assigned_by",
		"recurrence_template_ref",
		"tags",
	}),
	"documents": frozenset({
		"workspace",
		"project_key",
		"project_path",
		"parent_ref",
		"parent_title",
		"status",
		"status_category",
		"status_is_default",
		"status_label",
		"type",
		"type_label",
		"type_category",
		"type_is_default",
		"tags",
	}),
	"comments": frozenset({"author"}),
	"links": frozenset({"link_type", "label", "link_category", "source", "target"}),
	"verifications": frozenset({"task_ref", "recorded_by"}),
	"events": frozenset({"item_ref"}),
	"tags": frozenset(),
	"statuses": frozenset(),
	"item_types": frozenset(),
	"link_types": frozenset(),
	"saved_views": frozenset({"owner"}),
	"users": frozenset({"account_parent"}),
	"members": frozenset({"user", "role", "workspace"}),
	"project_members": frozenset({"user", "project"}),
}


def _stored (kind: str) -> set[str]:
	"""Return the columns of a kind's table: what is stored, not what the mapper computes."""

	return {column.key for column in KIND_TABLES[kind].__table__.columns}


@pytest.mark.parametrize("kind", list(subroutine.views.EXPORTED))
def test_every_field_of_every_kind_is_kept_or_left_out_on_purpose (kind: str) -> None:
	"""`#4053`: a field no column holds is either a name kept, or a working-out left out.

	Read off the table rather than the mapper, because ``rank`` and ``relevance`` are mapped
	expressions on a task and stored nowhere.
	"""

	fields = set(subroutine.views.EXPORTED[kind].model_fields)
	left_out = subroutine.exporting.COMPUTED[kind]
	unplaced = sorted(fields - _stored(kind) - left_out - NAMED[kind])

	assert not unplaced, (
		f"{kind} reports {unplaced}, which no column of its table holds. Keep each as a name "
		f"in NAMED here, or leave it out in exporting.COMPUTED."
	)
	assert left_out <= fields and NAMED[kind] <= fields, f"{kind} names a field it has not got"
	assert not left_out & NAMED[kind], f"{kind} both keeps and leaves out {left_out & NAMED[kind]}"
	assert not (left_out | NAMED[kind]) & _stored(kind), f"{kind} classifies a stored column"


def test_the_classification_can_see_a_field_nobody_placed () -> None:
	"""Fed a task view with one field more, through the same comparison."""

	class Grown(subroutine.views.Task):
		"""A task view as it might be one release on."""

		momentum: int = 0

	fields = set(Grown.model_fields)
	unplaced = fields - _stored("tasks") - subroutine.exporting.COMPUTED["tasks"] - NAMED["tasks"]

	assert unplaced == {"momentum"}


def test_a_line_keeps_what_was_stored_and_names_and_drops_what_was_worked_out (
	pair: Pair,
) -> None:
	"""A task's line has its fields and the names beside its ids, and no ranking or flag."""

	made = _seeded(pair)
	(fix,) = [row for row in pair.local.export("tasks") if row.ref == made["fix"].ref]
	written = subroutine.exporting.line("tasks", fix)

	assert {"id", "ref", "title", "status", "status_id", "project_key", "created_by"} <= set(written)
	assert not {"blocked", "rank", "relevance", "priority_score", "project_colour"} & set(written)


def test_a_link_s_ends_say_which_item_and_nothing_of_its_state (pair: Pair) -> None:
	"""The item's own file holds its state as stored; an end holds it worked out for a reader."""

	_seeded(pair)
	(edge,) = list(pair.local.export("links"))
	written = subroutine.exporting.line("links", edge)

	for end in ("source", "target"):
		assert set(written[end]) == set(subroutine.exporting.END_KEPT), written[end]

	assert written["created_by"] == str(pair.user.id) and written["label"]


def test_a_membership_says_whose_and_where_and_nothing_of_their_state (pair: Pair) -> None:
	"""`#4075`: a member's line keeps which account it is and its name, as a link's end keeps an item.

	The account is in ``users``, as it was stored, and the workspace in ``workspace``. A project
	membership says which project by its id too, since a key is unique only under one parent.
	"""

	_seeded(pair)
	(member,) = list(pair.local.export("members"))
	written = subroutine.exporting.line("members", member)

	assert set(written["user"]) == set(subroutine.exporting.ACCOUNT_KEPT), written["user"]
	assert written["user"]["id"] == str(pair.user.id)
	assert set(written["workspace"]) == {"id", "slug"}, written["workspace"]
	assert written["role"] == "owner" and written["created_at"]

	web = next(row for row in pair.local.export("projects") if row.key == "web")
	(shared,) = [row for row in pair.local.export("project_members") if row.project == "web"]
	written = subroutine.exporting.line("project_members", shared)

	assert set(written["user"]) == set(subroutine.exporting.ACCOUNT_KEPT), written["user"]
	assert written["project_id"] == str(web.id)


def test_the_workspace_is_one_line_of_its_own_row (pair: Pair) -> None:
	"""Its time zone, description, settings and prioritised project, which no other file holds."""

	_seeded(pair)
	pair.local.update_workspace(
		pair.workspace.slug,
		description="Where the website rebuild lives.",
		timezone="Europe/London",
		prioritised_project="web",
	)
	(own,) = [
		subroutine.exporting.line("workspace", row) for row in pair.remote.export("workspace")
	]

	assert own["id"] == str(pair.workspace.id) and own["slug"] == pair.workspace.slug
	assert own["description"] == "Where the website rebuild lives."
	assert own["timezone"] == "Europe/London" and own["prioritised_project"] == "web"
	assert isinstance(own["settings"], dict)
