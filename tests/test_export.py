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
	it and what happened to it - each is in the owner's export and none in the colleague's.
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
	and with it, a comment on a task still goes only to somebody who may read the task.
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
