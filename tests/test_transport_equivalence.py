"""The same scenarios through both transports, asserting the output matches.

SPEC.md §13.7 makes the local database a connection like any other so that ``subroutine
today`` fans out across it and every configured remote through **one** code path — one that
does not know which of its answers arrived over a socket. That claim is only true while the
two clients return the same objects, and it is the kind of claim that stops being true
quietly: a field added to a view, an ordering changed in one listing, an error raised as a
different class.

So both clients are pointed at the **same database** in the same test, and their answers are
compared field by field. The HTTP one goes through the real application over httpx's ASGI
transport; nothing here is stubbed, because a stub would agree with whatever it was written
against.

Every test runs on SQLite and PostgreSQL, since two of the things most likely to diverge —
NULL ordering and datetime awareness — are invisible on one of them.
"""

import datetime
import json
import subprocess
import sys
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.comments
import subroutine.domain.documents
import subroutine.domain.links
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions
import subroutine.views


class Pair(typing.NamedTuple):
	"""The same installation, reached both ways."""

	local: subroutine.clients.local.Client
	remote: subroutine.clients.http.Client
	session: sqlalchemy.orm.Session
	user: subroutine.db.models.identity.User
	workspace: subroutine.db.models.identity.Workspace

	def both (self) -> tuple[subroutine.clients.local.Client, subroutine.clients.http.Client]:
		"""Return the two clients, for a test that asks each the same thing."""

		return self.local, self.remote


@pytest.fixture
def pair (session: sqlalchemy.orm.Session) -> typing.Iterator[Pair]:
	"""One installation, a local client and an HTTP client, sharing a transaction."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test Instance"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Equivalence"
	)
	session.flush()

	factory = api_support.factory_for(session)
	settings = subroutine.config.Settings(dev_mode=True)
	application = api_support.build_app(factory)

	local = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"), settings, session_factory=factory
	)
	remote = subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://tasks.example.com"),
		token=issued.value.get_secret_value(),
		transport=api_support.SyncTransport(application),
		base_url=api_support.BASE_URL,
	)

	with local, remote:
		yield Pair(
			local=local,
			remote=remote,
			session=session,
			user=setup.user,
			workspace=setup.workspace,
		)


def make (pair: Pair, text: str) -> subroutine.views.Task:
	"""Add a task through the local client, for the other one to read back."""

	return pair.local.capture(text=text).task


def test_both_report_the_same_instance_and_workspaces (pair: Pair) -> None:
	"""``identity`` is what tells two connections apart, so it has to agree with itself."""

	local, remote = pair.both()

	assert local.identity() == remote.identity()

	instance = local.identity().instance

	assert instance is not None
	assert instance.name == "Test Instance"
	assert [workspace.slug for workspace in local.identity().workspaces] == [
		pair.workspace.slug
	]


def test_both_answer_who_is_asking (pair: Pair) -> None:
	"""``me`` is the other identity question, and the two must not be confused (`#336`).

	``identity`` describes the *instance*; this describes the *principal*. They come apart
	exactly where several agents share one machine: one connection, one instance, and a
	different answer here for each of them.

	The credential is the one field that legitimately differs between these two clients,
	because the local one was opened without a token — §12.1a says the filesystem permission
	is the authentication there. Everything about *who* is asking has to agree.
	"""

	local, remote = pair.both()
	mine, theirs = local.me(), remote.me()

	assert mine.user == theirs.user
	assert mine.api_version == theirs.api_version
	assert mine.instance_permissions == theirs.instance_permissions
	assert mine.workspaces == theirs.workspaces

	assert mine.credential is None
	assert theirs.credential is not None
	assert theirs.credential.title == "Equivalence"


def test_a_narrowed_credential_narrows_the_answer_on_both_transports (
	pair: Pair, session: sqlalchemy.orm.Session
) -> None:
	"""The claim the whole agent-identity milestone rests on, asserted rather than assumed.

	An agent is told what it may do so that it does not have to find out by being refused
	(§13.1). That is only worth anything if the answer is the same whichever transport it
	asked through — a local client reporting an unnarrowed principal for a scoped token would
	tell an agent it may write, and the write would then be refused by the same installation.
	"""

	inbox = subroutine.domain.bootstrap.inbox_for(session, pair.workspace)

	assert inbox is not None

	_row, issued = subroutine.domain.authentication.issue_token(
		session,
		user=pair.user,
		title="Narrow",
		scopes=[subroutine.permissions.TASK_READ],
		project_scope=[str(inbox.id)],
		workspace_id=pair.workspace.id,
	)
	session.flush()

	secret = issued.value.get_secret_value()
	factory = api_support.factory_for(session)
	local = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=factory,
		token=secret,
	)
	remote = subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://tasks.example.com"),
		token=secret,
		transport=api_support.SyncTransport(api_support.build_app(factory)),
		base_url=api_support.BASE_URL,
	)

	with local, remote:
		mine, theirs = local.me(), remote.me()

	# **Everything but ``last_used_at``, which cannot agree and should not.** Presenting a
	# credential stamps it, so the second of these two calls always reports a later moment than
	# the first. That field is not noise: watching it move is how the nuc14 agent worked out
	# which principal its MCP server was using, before anything could be asked directly
	# (`#335`) — which is the gap this method closes.
	assert mine.user == theirs.user
	assert mine.workspaces == theirs.workspaces
	assert mine.instance_permissions == theirs.instance_permissions
	assert theirs.credential is not None
	assert mine.credential == theirs.credential.model_copy(
		update={"last_used_at": None if mine.credential is None else mine.credential.last_used_at}
	)

	assert mine.credential is not None
	assert mine.credential.narrows is True
	assert mine.credential.scopes == [subroutine.permissions.TASK_READ]
	assert mine.credential.workspace_id == pair.workspace.id

	# **The key is resolved by the instance, so both transports report it** (`#216`). The ids
	# are what is stored and what the API takes; the key is what somebody typed, and a client
	# that had to ask separately would pay a second call to read back what it just set.
	assert mine.credential.project_scope == [str(inbox.id)]
	assert mine.credential.project_scope_keys == [inbox.key]

	# The permissions are the field a caller acts on, and they are the *intersection* rather
	# than the role — a contributor who may write is reported as unable to, because the
	# credential says so (§7.3).
	assert [workspace.permissions for workspace in mine.workspaces] == [
		[subroutine.permissions.TASK_READ]
	]
	assert all(workspace.narrowed_by_credential for workspace in mine.workspaces)


def test_both_administer_credentials_the_same_way (pair: Pair) -> None:
	"""`#348`: the three commands that set an agent up now go through a connection.

	They opened a local database directly, because §12.4 requires the administrative commands
	to work when the service will not start. That is right and it assumed there *is* a local
	database — so on a machine whose work lives on a served instance, the three commands you
	need in order to set an agent up were the three that could not run.
	"""

	local, remote = pair.both()

	issued = remote.issue_token(title="Over the wire", scopes=["task:read"])

	assert issued.token.startswith("sr_")
	assert issued.title == "Over the wire"
	assert issued.scopes == ["task:read"]
	assert issued.narrows is True

	# Read back through the *other* transport, which is the whole claim: one inventory, two
	# ways of asking.
	mine = {row.prefix: row for row in local.tokens()}

	assert issued.prefix in mine
	assert mine[issued.prefix].title == "Over the wire"
	assert mine[issued.prefix].usable is True

	# **The secret is in the issuing response and in nothing else, ever** (§7.4).
	assert issued.token not in json.dumps(
		[row.model_dump(mode="json") for row in local.tokens()]
	)

	stopped = local.revoke_token(id_or_prefix=issued.prefix)

	assert stopped.revoked_at is not None
	assert {row.prefix: row.usable for row in remote.tokens()}[issued.prefix] is False


def test_both_keep_the_first_revocation_time (pair: Pair) -> None:
	"""Revoking twice is not an error and does not move the instant.

	When a credential stopped being trusted is a fact worth not overwriting, and a caller
	retrying a request it is unsure landed should not change it.
	"""

	local, remote = pair.both()
	issued = local.issue_token(title="Twice")

	first = local.revoke_token(id_or_prefix=issued.prefix)
	again = remote.revoke_token(id_or_prefix=issued.prefix)

	assert first.revoked_at is not None
	assert again.revoked_at == first.revoked_at


def test_both_create_a_service_account_and_its_credential_in_one_call (pair: Pair) -> None:
	"""Three writes — an account, a membership, a credential — as one call and one transaction.

	Over a network the alternative is three requests and a half-finished agent if the second
	fails: an account that authenticates and can do nothing, which reads as a broken token
	rather than as a missing membership.
	"""

	local, remote = pair.both()
	issued = remote.issue_token(service_account="claude", title="An agent")

	assert issued.account_created is True
	assert issued.username == "claude"

	# It is a machine identity on both sides of the socket, and it can actually work.
	assert [row.is_service_account for row in local.users() if row.username == "claude"] == [
		True
	]

	# Naming it again reuses the account rather than refusing: a second token for one agent is
	# an ordinary thing to want.
	assert remote.issue_token(service_account="claude").account_created is False


def test_both_refuse_to_hand_out_a_persons_credential_under_a_machine_argument (
	pair: Pair,
) -> None:
	"""`#207`, now enforced in the service so both transports refuse identically."""

	local, remote = pair.both()

	for client in (local, remote):
		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			client.issue_token(service_account=pair.user.username)

		assert "not a machine identity" in str(refused.value)


def test_both_render_a_task_identically (pair: Pair) -> None:
	"""Every field, not a chosen few — including the ones only one path computes."""

	created = make(pair, "Ship the release by friday !3 ~2h #urgent #work")

	# Urgency has no capture token (§6.13 covers importance only), and this is here to make
	# `priority_score` a *derived* number on both sides rather than a null that would agree
	# by costing nobody anything.
	pair.session.execute(
		sqlalchemy.update(subroutine.db.models.work.Task)
		.where(subroutine.db.models.work.Task.id == created.id)
		.values(urgency=3)
	)
	pair.session.flush()

	local, remote = pair.both()
	from_local = local.task(ref=created.ref)
	from_remote = remote.task(ref=created.ref)

	assert from_local == from_remote
	assert from_local is not None

	# The three fields most likely to be produced by only one of the two paths: a derived
	# value, a batch-loaded relation, and a resolved vocabulary key.
	assert from_local.priority_score == 9
	assert from_local.tags == ["urgent", "work"]
	assert from_local.status_category == "todo"


def test_both_list_the_same_rows_in_the_same_order (pair: Pair) -> None:
	"""Ordering is where a listing diverges without anybody noticing.

	Ten rows created in one transaction share a ``created_at`` on a fast machine, so this is
	also the tie-break test — and the tie-break is a different column on each side unless
	somebody kept them the same.
	"""

	for index in range(10):
		make(pair, f"Task number {index}")

	local, remote = pair.both()

	assert local.tasks() == remote.tasks()
	assert [task.title for task in local.tasks()] == [
		task.title for task in remote.tasks()
	]


def test_both_honour_the_same_page_size (pair: Pair) -> None:
	"""Equivalence over the *parameters*, not just the methods.

	This is where the claim was actually false. `local.tasks(limit=1000)` returned every row
	and `remote.tasks(limit=1000)` returned `max_page_size`, because each side had its own copy
	of "how big is a page" — and the test above compares default arguments only, so it could not
	notice. Both now go through `domain.paging.size`.
	"""

	settings = subroutine.config.Settings(dev_mode=True)
	beyond = settings.max_page_size + 5

	for index in range(beyond):
		make(pair, f"Task number {index}")

	local, remote = pair.both()

	assert len(local.tasks(limit=beyond)) == settings.max_page_size
	assert local.tasks(limit=beyond) == remote.tasks(limit=beyond)
	assert local.tasks(limit=3) == remote.tasks(limit=3)
	assert len(local.tasks(limit=3)) == 3


@pytest.mark.parametrize("refused", [0, -1])
def test_both_refuse_a_page_size_nothing_could_honour (pair: Pair, refused: int) -> None:
	"""And they refuse it the same way, rather than one guessing.

	`limit=-1` meant *no limit* on SQLite and raised a bare ``DataError`` on PostgreSQL — a
	non-``SubroutineError`` that escaped the fan-out's containment, so one bad flag took down
	every connection. `limit=0` quietly meant "the default".
	"""

	local, remote = pair.both()

	for client in (local, remote):
		with pytest.raises(subroutine.errors.ValidationError) as raised:
			client.tasks(limit=refused)

		assert raised.value.errors[0].field == "limit"


def test_both_span_completed_tasks_the_same_way (pair: Pair) -> None:
	"""The HTTP client spells this parameter itself, and nothing compared the two spellings."""

	created = make(pair, "Finish this one")
	local, remote = pair.both()
	local.complete(ref=created.ref)

	assert local.tasks() == remote.tasks() == []
	assert local.tasks(include_completed=True) == remote.tasks(include_completed=True)
	assert len(local.tasks(include_completed=True)) == 1


def test_both_take_a_named_workspace_the_same_way (pair: Pair) -> None:
	"""`workspace=` was never passed to any method by any equivalence test."""

	created = make(pair, "In the named workspace")
	slug = pair.workspace.slug
	local, remote = pair.both()

	assert local.tasks(workspace=slug) == remote.tasks(workspace=slug)
	assert local.task(ref=created.ref, workspace=slug) == remote.task(
		ref=created.ref, workspace=slug
	)

	# And an unknown one is refused identically, rather than answered emptily by one of them.
	for client in (local, remote):
		with pytest.raises(subroutine.errors.NotFound):
			client.tasks(workspace="no-such-workspace")


def test_both_return_the_same_agenda (pair: Pair) -> None:
	"""Four buckets, a date, a zone and a total, all resolved the same way."""

	make(pair, "Overdue thing by 2020-01-01")
	make(pair, "Today thing by today")
	make(pair, "Someday thing")

	local, remote = pair.both()
	day = datetime.date(2026, 7, 30)

	assert local.agenda(date=day, timezone="Europe/London") == remote.agenda(
		date=day, timezone="Europe/London"
	)
	assert local.agenda(date=day).date == day

	# Narrowing has to agree too, including how each refuses a workspace that is not there —
	# a filter one client honours and the other ignores is the divergence S3-07 removed.
	assert local.agenda(date=day, workspace=pair.workspace.slug) == remote.agenda(
		date=day, workspace=pair.workspace.slug
	)

	for client in (local, remote):
		with pytest.raises(subroutine.errors.NotFound):
			client.agenda(date=day, workspace="no-such-workspace")


def test_both_capture_the_same_line_the_same_way (pair: Pair) -> None:
	"""Including what the grammar declined to read, which each works out differently."""

	local, remote = pair.both()

	from_local = local.capture(text="Water the plants every monday")
	from_remote = remote.capture(text="Water the plants every monday")

	assert from_local.unparsed == from_remote.unparsed == ("every monday",)

	# §6.13 rule 1: nothing is lost, so the words stay in the title on both paths.
	assert from_local.task.title == from_remote.task.title == "Water the plants every monday"


def test_both_report_what_the_grammar_read_the_same_way (pair: Pair) -> None:
	"""``#135``, and the mirror of the test above.

	The two sides compute this differently and must not be able to differ: the local client
	has the parse in hand, and the HTTP client re-runs the grammar on the line because
	``POST /v1/tasks`` returns the task and nothing else (§8.4). One of them describing a
	capture the other would describe another way is the divergence §13.7 exists to prevent.
	"""

	local, remote = pair.both()
	line = "Fix the header !4/2 ~2h #ops"

	assert local.capture(text=line).summary == remote.capture(text=line).summary == (
		"!4/2 ~2h #ops"
	)

	# And silence stays silence on both, so an ordinary line is answered as it always was.
	assert local.capture(text="Buy milk").summary is None
	assert remote.capture(text="Buy milk").summary is None


def test_both_create_a_project_the_same_way (pair: Pair) -> None:
	"""``#134``. Until this landed there was no local path at all, so there was nothing to
	compare — a project could be made over HTTP and nowhere else, which on a default install
	means nowhere, because nothing runs ``serve`` unless somebody asks it to."""

	local, remote = pair.both()

	by_local = local.create_project(key="ALPHA", title="From the local client")
	by_remote = remote.create_project(key="BETA", title="From the HTTP client")

	assert by_local.key == "ALPHA"
	assert by_remote.key == "BETA"

	# Every field that is not the two they were told to differ in. A create is where a default
	# most easily comes to be decided in two places — `visibility` is passed explicitly by the
	# HTTP client for exactly that reason.
	differs = {"id", "key", "title", "created_at", "updated_at"}
	as_local = by_local.model_dump()
	as_remote = by_remote.model_dump()

	assert {name: value for name, value in as_local.items() if name not in differs} == {
		name: value for name, value in as_remote.items() if name not in differs
	}


def test_both_list_the_same_projects_parents_before_children (pair: Pair) -> None:
	"""Ordered by path, so the tree prints in one pass without the caller reassembling it."""

	local, remote = pair.both()

	above = local.create_project(key="OUTER", title="Outer")
	local.create_project(key="INNER", title="Inner", parent=above.key)

	from_local = [(one.key, one.depth) for one in local.projects()]
	from_remote = [(one.key, one.depth) for one in remote.projects()]

	assert from_local == from_remote
	assert from_local.index(("OUTER", 0)) < from_local.index(("INNER", 1))


def test_both_make_the_creator_the_owner_so_a_private_project_stays_visible (
	pair: Pair,
) -> None:
	"""§7.3a grants sight of a private project only to holders of a ``project_member`` row.

	``projects.create`` writes one for the owner, so **omitting the owner is how a private
	project becomes invisible to the person who made it** — which is a thing that shipped
	once already. The CLI has no way to name somebody else, so the only way to get this wrong
	is to leave it out.
	"""

	local, remote = pair.both()

	by_local = local.create_project(key="HUSH", title="Quiet", visibility="private")
	by_remote = remote.create_project(key="SHH", title="Quieter", visibility="private")

	assert by_local.owner_id == by_remote.owner_id == pair.user.id
	assert {one.key for one in local.projects()} >= {"HUSH", "SHH"}
	assert {one.key for one in remote.projects()} >= {"HUSH", "SHH"}


def test_both_refuse_an_unusable_key_the_same_way (pair: Pair) -> None:
	"""A key is permanent and becomes part of every address, so it is refused, never fixed up.

	The refusal has to be the same sentence on both transports: a person meeting it through
	the CLI and an agent meeting it over HTTP are being told about the same rule.
	"""

	local, remote = pair.both()
	refusals = []

	for client in (local, remote):
		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			client.create_project(key="2FA", title="Starts with a digit")

		refusals.append(refused.value)

	assert refusals[0].detail == refusals[1].detail
	assert refusals[0].code == refusals[1].code


def _blocks (pair: Pair, blocker: subroutine.views.Task, blocked: subroutine.views.Task) -> None:
	"""Record that one task cannot start until another is finished."""

	def end (task: subroutine.views.Task) -> subroutine.domain.links.End:
		"""Describe one side of the link."""

		return subroutine.domain.links.End(
			entity_type="task",
			id=task.id,
			ref=task.ref,
			title=task.title,
			project_id=task.project_id,
		)

	subroutine.domain.links.create(
		pair.session,
		workspace_id=pair.workspace.id,
		source=end(blocker),
		target=end(blocked),
		link_type_key="blocks",
	)
	pair.session.flush()


def test_both_narrow_to_ready_work_the_same_way (pair: Pair) -> None:
	"""``#136``. The one question this tool answers that a list of tasks does not (§6.5a).

	It reached ``GET /v1/tasks?ready=true`` and nothing else, so the two transports had never
	been compared on it — and the local one had no implementation at all to compare.
	"""

	first = make(pair, "Do this first")
	second = make(pair, "Then this")
	free = make(pair, "Unrelated")

	_blocks(pair, first, second)

	local, remote = pair.both()
	ready = {task.ref for task in local.tasks(ready=True)}

	assert ready == {task.ref for task in remote.tasks(ready=True)}
	assert second.ref not in ready, "blocked by something unfinished"
	assert {first.ref, free.ref} <= ready


def test_readiness_ignores_a_status_somebody_set_by_hand (pair: Pair) -> None:
	"""**The property most likely to be quietly changed**, and the one worth pinning (§5.5).

	A ``blocks`` link is a tracked dependency that resolves itself when the other side is
	finished. A status somebody typed is a declaration about the world, usually about
	something outside the system, and nothing here can tell when it stops being true. Folding
	the second into readiness would mean a filter that silently never returns work nobody
	remembered to un-declare.
	"""

	declared = make(pair, "Waiting on the supplier")

	local, remote = pair.both()
	local.update(ref=declared.ref, status="blocked")

	assert declared.ref in {task.ref for task in local.tasks(ready=True)}
	assert declared.ref in {task.ref for task in remote.tasks(ready=True)}


def test_readiness_excludes_work_deferred_to_a_later_date (pair: Pair) -> None:
	"""The other half of the predicate, and the reason it subsumes ``deferred``."""

	later = make(pair, "Chase it up next week")
	local, remote = pair.both()

	local.schedule(ref=later.ref, start=datetime.date.today() + datetime.timedelta(days=7))

	assert later.ref not in {task.ref for task in local.tasks(ready=True)}
	assert later.ref not in {task.ref for task in remote.tasks(ready=True)}


def test_both_write_a_document_the_same_way (pair: Pair) -> None:
	"""``#138``, and the half of §5.10 that could not be reached outside HTTP.

	A comment is what happened; a document is what you concluded. The second was writable only
	by ``POST /v1/documents`` — so on a default install, where nothing runs ``serve``, the
	practice this product is built around was half unavailable, while the MCP adapter's own
	tool description told agents to follow it.
	"""

	local, remote = pair.both()

	by_local = local.create_document(title="From here", body="Because.", type="decision")
	by_remote = remote.create_document(title="From there", body="Because.", type="decision")

	assert by_local.type == by_remote.type == "decision"
	assert by_local.body == by_remote.body == "Because."

	# Three timestamps, not two: `content_updated_at` moves with the body and so is set at
	# creation like the other two. Found by comparing every field rather than a chosen few,
	# which is the point of doing it that way.
	differs = {"id", "ref", "title", "created_at", "updated_at", "content_updated_at"}
	as_local = by_local.model_dump()
	as_remote = by_remote.model_dump()

	assert {name: value for name, value in as_local.items() if name not in differs} == {
		name: value for name, value in as_remote.items() if name not in differs
	}


def test_both_default_a_document_to_a_note_in_the_inbox (pair: Pair) -> None:
	"""Nothing but a title is required, because a conclusion arrives before its filing does."""

	local, remote = pair.both()

	assert local.create_document(title="Just a thought").type == "note"
	assert remote.create_document(title="Another thought").type == "note"


def test_both_attribute_a_document_to_whoever_wrote_it (pair: Pair) -> None:
	"""§5.10's "what you concluded" needs a *you*. A conclusion with no author is a rumour."""

	local, remote = pair.both()

	assert local.create_document(title="Mine").owner_id == pair.user.id
	assert remote.create_document(title="Also mine").owner_id == pair.user.id


def test_both_reclassify_an_item_the_same_way (pair: Pair) -> None:
	"""``#42``. What something is becomes clear after it has been looked at.

	It was settable at creation and nowhere else, so a task filed as a task could never become
	a bug — and on the surface an agent uses it could not be set at creation either, so
	*everything* an agent filed was a task for ever. That is not an edge case: it made the
	rule "the type is a promise about what the title says" one an agent physically could not
	follow.
	"""

	first = make(pair, "A distant date renders as if it were this year")
	second = make(pair, "Another one")

	local, remote = pair.both()

	assert local.update(ref=first.ref, type="bug").type == "bug"
	assert remote.update(ref=second.ref, type="bug").type == "bug"

	# And back again, because reclassifying is not a one-way door either.
	assert local.update(ref=first.ref, type="feature").type == "feature"


def test_both_file_with_a_type_in_one_call (pair: Pair) -> None:
	"""So the type is right when it is filed, rather than one call and one memory later.

	``type`` rides beside the captured line rather than inside it: §6.13's sigils are for
	things somebody types mid-sentence, and "this is a bug" is a classification *about* the
	sentence rather than part of it.
	"""

	local, remote = pair.both()

	assert local.capture(text="Fix the header", type="bug").task.type == "bug"
	assert remote.capture(text="Fix the footer", type="bug").task.type == "bug"

	# Unset still means the default, so an ordinary capture is unchanged.
	assert local.capture(text="Buy milk").task.type == "task"


def test_reclassifying_leaves_the_status_where_it_was (pair: Pair) -> None:
	"""**The decision inside `#42`**, and the one worth pinning.

	A type carries a default status set at creation. Dragging the status along with a later
	type change would move a half-finished bug back to "open" because somebody corrected its
	classification — a second change, unasked, wearing the first one's clothes.
	"""

	task = make(pair, "Half done already")
	local, _remote = pair.both()

	local.update(ref=task.ref, status="in_progress")

	assert local.update(ref=task.ref, type="bug").status == "in_progress"


def test_both_refuse_a_type_that_does_not_exist (pair: Pair) -> None:
	"""And identically, because a vocabulary is a contract and a typo is the common case."""

	task = make(pair, "Something")
	local, remote = pair.both()
	refusals = []

	for client in (local, remote):
		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			client.update(ref=task.ref, type="epic")

		refusals.append(refused.value)

	assert refusals[0].detail == refusals[1].detail


def test_a_type_change_is_recorded_as_something_that_happened (pair: Pair) -> None:
	"""``_snapshot`` decides both what an event says and whether one is written at all.

	A field it forgets is a change that bumps the version and leaves no trace — which is how
	``urgency`` shipped a column, a constraint, a sort key and a compact-line cell without an
	event for a day. Adding a writable field means adding it there too.
	"""

	task = make(pair, "Reclassify me")
	local, _remote = pair.both()

	local.update(ref=task.ref, type="bug")

	events = pair.session.scalars(
		sqlalchemy.select(subroutine.db.models.activity.Event).where(
			subroutine.db.models.activity.Event.entity_id == task.id
		)
	).all()
	changed = [name for event in events for name in (event.changes or {})]

	assert "type_id" in changed, changed


def test_both_move_an_item_to_the_trash_and_back (pair: Pair) -> None:
	"""``#140``. Nothing added by mistake could be removed, on a personal to-do list.

	`done` was the only way to make something go away, and it is a lie: it says the thing
	happened. And the restore half had been promised in three places — §6.9's "restorable for a
	configurable retention period", the `trash_retention_days` setting, and
	`EventAction.RESTORED` — with nothing anywhere clearing `deleted_at`.
	"""

	mistake = make(pair, "Buy mikl")
	other = make(pair, "Call the dentist")

	local, remote = pair.both()

	assert local.discard(ref=mistake.ref).deleted_at is not None
	assert remote.discard(ref=other.ref).deleted_at is not None
	assert {task.ref for task in local.tasks()} == set()

	assert local.undiscard(ref=mistake.ref).deleted_at is None
	assert remote.undiscard(ref=other.ref).deleted_at is None
	assert {task.ref for task in local.tasks()} == {mistake.ref, other.ref}


def test_both_list_the_trash_and_only_the_trash (pair: Pair) -> None:
	"""A mixed list is the one place nothing in a row says which kind of thing it is."""

	gone = make(pair, "Deleted")
	kept = make(pair, "Kept")

	local, remote = pair.both()
	local.discard(ref=gone.ref)

	assert [task.ref for task in local.tasks(deleted=True)] == [gone.ref]
	assert [task.ref for task in remote.tasks(deleted=True)] == [gone.ref]
	assert [task.ref for task in local.tasks()] == [kept.ref]


def test_both_find_a_deleted_item_when_asked_for_it_by_ref (pair: Pair) -> None:
	"""**A live divergence**, found by building `restore` and watching it fail.

	`api/tasks._resolve` has always included the trash — "a reference to something in the trash
	is more useful than a dangling one" — and the local client's `_row` did not. So
	`client.task(ref=…)` answered one question with the task over HTTP and `None` locally.
	Nothing noticed because nothing had ever looked one up after deleting it: there was no way
	to delete one.
	"""

	task = make(pair, "About to go")
	local, remote = pair.both()

	local.discard(ref=task.ref)

	assert local.task(ref=task.ref) == remote.task(ref=task.ref)
	assert local.task(ref=task.ref) is not None


def test_both_take_a_document_to_the_trash_too (pair: Pair) -> None:
	"""One counter serves both kinds (§6.2), so an operation on half the numbers is a trap."""

	local, remote = pair.both()
	written = local.create_document(title="Written by mistake")

	discarded = local.discard(ref=written.ref, entity_type="document")

	assert discarded.deleted_at is not None
	assert local.undiscard(ref=written.ref, entity_type="document").deleted_at is None
	assert [one.ref for one in remote.documents()] == [written.ref]


def test_discarding_twice_is_not_an_error_and_does_not_move_the_timestamp (
	pair: Pair,
) -> None:
	"""Symmetrically with restoring twice. When something was thrown away is a fact.

	A caller retrying a request it is unsure landed must not change the answer, which is the
	same reason `complete` is unconditional.
	"""

	task = make(pair, "Gone")
	local, _remote = pair.both()

	first = local.discard(ref=task.ref)
	again = local.discard(ref=task.ref)

	assert first.deleted_at == again.deleted_at

	local.undiscard(ref=task.ref)

	assert local.undiscard(ref=task.ref).deleted_at is None


def test_both_join_two_items_the_same_way (pair: Pair) -> None:
	"""``#141``'s highest, because ``blocks`` is what readiness reads (§6.5a).

	Until this landed an agent could ask what was startable and could not say what blocked
	what — the filter existed and nothing but raw HTTP could put anything into it.
	"""

	first = make(pair, "Design the schema")
	second = make(pair, "Build the endpoint")
	third = make(pair, "Unrelated")

	local, remote = pair.both()
	made = local.link(ref=first.ref, link_type="blocks", target=second.ref)

	assert made.other.ref == second.ref
	assert made.direction == "outgoing"

	# The whole point: the link is what `ready` reads, on both transports.
	assert {task.ref for task in local.tasks(ready=True)} == {first.ref, third.ref}
	assert {task.ref for task in remote.tasks(ready=True)} == {first.ref, third.ref}


def test_both_withdraw_a_link_the_same_way (pair: Pair) -> None:
	"""It follows ``link`` closely rather than waiting for somebody to ask.

	A link added by mistake blocks work that is not blocked, and readiness then hides it — so
	an unwanted link is worse than a missing one, because it narrows what looks startable and
	says nothing about having done so.
	"""

	first = make(pair, "Blocker")
	second = make(pair, "Blocked")

	local, remote = pair.both()
	made = local.link(ref=first.ref, link_type="blocks", target=second.ref)

	assert second.ref not in {task.ref for task in local.tasks(ready=True)}

	remote.unlink(ref=first.ref, link_id=str(made.id))

	assert second.ref in {task.ref for task in local.tasks(ready=True)}
	assert local.links(ref=first.ref) == remote.links(ref=first.ref) == []


def test_both_refuse_a_link_type_nobody_seeded (pair: Pair) -> None:
	"""And name the ones that exist, because a vocabulary is only usable if it is listed."""

	first = make(pair, "One")
	second = make(pair, "Two")

	local, remote = pair.both()
	refusals = []

	for client in (local, remote):
		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			client.link(ref=first.ref, link_type="supersedes", target=second.ref)

		refusals.append(refused.value)

	assert refusals[0].detail == refusals[1].detail
	assert "blocks" in str(refusals[0].errors[0].hint)


def test_linking_twice_is_not_an_error (pair: Pair) -> None:
	"""Idempotent by (source, target, type), like the service beneath it.

	A client retrying a request it is unsure landed should not find out by being refused.
	"""

	first = make(pair, "One")
	second = make(pair, "Two")

	local, _remote = pair.both()

	assert local.link(ref=first.ref, link_type="blocks", target=second.ref).id == (
		local.link(ref=first.ref, link_type="blocks", target=second.ref).id
	)


def test_both_complete_a_task_the_same_way (pair: Pair) -> None:
	"""And both do it unconditionally, so "already done" stays the caller's decision."""

	first = make(pair, "Finish this one")
	second = make(pair, "And this one")

	local, remote = pair.both()
	by_local = local.complete(ref=first.ref)
	by_remote = remote.complete(ref=second.ref)

	assert by_local.completed_at is not None
	assert by_remote.completed_at is not None
	assert by_local.status == by_remote.status
	assert by_local.status_category == by_remote.status_category == "done"


def test_both_schedule_a_task_the_same_way (pair: Pair) -> None:
	"""And both tell an omitted field from a null one (§8.3)."""

	first = make(pair, "Plan this one")
	second = make(pair, "And this one")
	day = datetime.date(2026, 8, 3)

	local, remote = pair.both()

	assert local.schedule(ref=first.ref, planned_for=day).planned_for == day
	assert remote.schedule(ref=second.ref, planned_for=day).planned_for == day

	# Setting the other field must leave the first alone — the difference between "not
	# mentioned" and "cleared", which is the bug §8.3 exists to prevent.
	assert local.schedule(ref=first.ref, start=day).planned_for == day
	assert remote.schedule(ref=second.ref, start=day).planned_for == day

	assert local.schedule(ref=first.ref, planned_for=None).planned_for is None
	assert remote.schedule(ref=second.ref, planned_for=None).planned_for is None


def test_both_say_nothing_is_there_the_same_way (pair: Pair) -> None:
	"""A missing task is ``None`` on both sides, not a refusal on one of them.

	This is what makes resolving an address across several connections possible at all: the
	client asks every connection and expects most of them to say no.
	"""

	local, remote = pair.both()

	assert local.task(ref=9999) is None
	assert remote.task(ref=9999) is None


def test_a_remote_refusal_arrives_as_the_local_exception (pair: Pair) -> None:
	"""A problem document is read back into the class that would have raised it.

	Otherwise a client fanning out has two vocabularies of failure, and every message it
	prints has to say which kind it was before saying what went wrong.
	"""

	local, remote = pair.both()

	with pytest.raises(subroutine.errors.NotFound) as locally:
		local.complete(ref=4242)

	with pytest.raises(subroutine.errors.NotFound) as remotely:
		remote.complete(ref=4242)

	assert locally.value.code == remotely.value.code == "not_found"
	assert locally.value.status == remotely.value.status == 404


def test_a_remote_refusal_keeps_its_field_errors (pair: Pair) -> None:
	"""The part an agent acts on is the field name, and it has to survive the wire.

	Written as a live call rather than a hand-built problem document, so that a change to
	either end of the envelope breaks it.
	"""

	local, remote = pair.both()
	too_long = "x" * (subroutine.domain.tasks.MAX_TITLE_LENGTH + 1)

	with pytest.raises(subroutine.errors.PayloadTooLarge) as locally:
		local.capture(text=too_long)

	with pytest.raises(subroutine.errors.PayloadTooLarge) as remotely:
		remote.capture(text=too_long)

	assert locally.value.code == remotely.value.code
	assert locally.value.detail == remotely.value.detail
	assert [field.field for field in remotely.value.errors] == [
		field.field for field in locally.value.errors
	] == ["title"]


@pytest.mark.parametrize("transport", ["local", "remote"])
def test_a_read_only_connection_refuses_every_write_before_it_leaves (
	session: sqlalchemy.orm.Session, transport: str
) -> None:
	"""Client-side enforcement, which is the only place it can be (§13.7).

	Pointing an agent at a company instance for context while forbidding it to write there
	is a reasonable posture, and it is not one the company's server can arrange on the
	agent-owner's behalf.

	**Parameterised over both transports, because it was not.** This test existed, passed, and
	exercised the *local* client — where the setting is nearly pointless — while its own
	docstring described the remote case. The HTTP client had no check at all, so a
	``read_only = true`` employer's instance accepted ``subroutine add``. A true assertion that
	proved nothing, which is the failure mode the slice-2 review was written about.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Read only"
	)
	session.flush()

	factory = api_support.factory_for(session)
	client: subroutine.clients.base.Client

	if transport == "local":
		client = subroutine.clients.local.Client(
			subroutine.connections.Connection(name="local", read_only=True),
			subroutine.config.Settings(dev_mode=True),
			session_factory=factory,
		)

	else:
		client = subroutine.clients.http.Client(
			subroutine.connections.Connection(
				name="work", url="https://employer.example.com", read_only=True
			),
			token=issued.value.get_secret_value(),
			transport=api_support.SyncTransport(api_support.build_app(factory)),
			base_url=api_support.BASE_URL,
		)

	with client:
		# Every write, not just the first. A check added to `capture` alone would leave the
		# other two open, and nothing would say so.
		for attempt in (
			lambda: client.capture(text="This should not be written"),
			lambda: client.complete(ref=1),
			lambda: client.schedule(ref=1, planned_for=datetime.date(2026, 8, 3)),
		):
			with pytest.raises(subroutine.errors.Forbidden) as raised:
				attempt()

			assert "read-only" in raised.value.detail

	# And nothing was written by any of them.
	assert (
		session.scalar(
			sqlalchemy.select(sqlalchemy.func.count()).select_from(
				subroutine.db.models.work.Task
			)
		)
		== 0
	)


def test_the_shared_views_do_not_pull_in_a_web_framework () -> None:
	"""The invariant the whole `views.py` move exists for, held by a test rather than by prose.

	`views.py` was moved out of the `api` package so both clients could return the same objects.
	Nothing enforced the other half of that — that importing it costs a CLI nothing — and the
	rule was stated twice in docstrings and checked nowhere. Measured: `import fastapi` is 0.23s
	and 371 modules against a 0.50s CLI start, so a regression makes every `subroutine add`
	roughly 45% slower and changes no test result.

	Run in a subprocess because the suite itself imports FastAPI, so `sys.modules` in *this*
	process can never answer the question.
	"""

	probe = (
		"import sys; import subroutine.views, subroutine.cli.main; "
		"bad = sorted(n for n in sys.modules if n.split('.')[0] in {'fastapi', 'starlette'}); "
		"print('|'.join(bad))"
	)
	done = subprocess.run(
		[sys.executable, "-c", probe], capture_output=True, text=True, check=True
	)

	assert done.stdout.strip() == "", (
		"importing subroutine.views or the CLI now loads a web framework: "
		f"{done.stdout.strip()}"
	)


def test_both_render_a_link_identically (pair: Pair) -> None:
	"""Links crossed the transport boundary for the first time with ``subroutine show``.

	The reason this test exists at all: the link view lived inside ``api/documents.py``, so
	the HTTP client had a definition of a link and the local one had none. Moving it to
	``views.py`` is what makes the two the same object rather than two that agree — the same
	requirement, and the same fix, as S3-07 applied to a task.
	"""

	blocker = make(pair, "Do this first")
	blocked = make(pair, "Then this")

	subroutine.domain.links.create(
		pair.session,
		workspace_id=pair.workspace.id,
		source=subroutine.domain.links.End(
			entity_type="task",
			id=blocker.id,
			ref=blocker.ref,
			title=blocker.title,
			project_id=blocker.project_id,
		),
		target=subroutine.domain.links.End(
			entity_type="task",
			id=blocked.id,
			ref=blocked.ref,
			title=blocked.title,
			project_id=blocked.project_id,
		),
		link_type_key="blocks",
	)
	pair.session.flush()

	local, remote = pair.both()

	assert local.links(ref=blocker.ref) == remote.links(ref=blocker.ref)
	assert [link.label for link in local.links(ref=blocker.ref)] == ["Blocks"]

	# And the same row read from the other end, which is the half a client could invert.
	assert local.links(ref=blocked.ref) == remote.links(ref=blocked.ref)
	assert [link.label for link in local.links(ref=blocked.ref)] == ["Blocked by"]


def test_both_read_and_write_the_record_of_what_happened (pair: Pair) -> None:
	"""Comments through both transports, written by one and read by the other.

	Written locally and read remotely on purpose: a comment created through the service layer
	and one created through the router must be the same row, or ``subroutine comment`` and an
	agent's ``POST`` would produce records that only look alike.
	"""

	task = make(pair, "Fix the parser")

	local, remote = pair.both()
	written = local.remark(ref=task.ref, body="Ran the suite: two failures.")

	assert remote.comments(ref=task.ref) == local.comments(ref=task.ref)
	assert [item.body for item in remote.comments(ref=task.ref)] == [written.body]

	# The other direction, and the ordering that makes a record a record.
	remote.remark(ref=task.ref, body="Both were in the date parser.")

	assert [item.body for item in local.comments(ref=task.ref)] == [
		"Ran the suite: two failures.",
		"Both were in the date parser.",
	]


def test_both_refuse_a_ref_that_names_nothing_the_same_way (pair: Pair) -> None:
	"""A refusal is part of the interface, and the two used to raise different classes."""

	local, remote = pair.both()

	for client in (local, remote):
		with pytest.raises(subroutine.errors.NotFound):
			client.comments(ref=9999)

	assert local.document(ref=9999) is None
	assert remote.document(ref=9999) is None


def test_neither_transport_reads_an_item_a_token_may_not_see (pair: Pair) -> None:
	"""The new read surface, narrowed the same way everything else is (SPEC.md §7.3).

	``show`` gave the clients three new ways into the database — a document by ref, an item's
	links and its record of what happened — and each is a point lookup by ref, which is the
	shape most likely to be written as a direct query with the narrowing forgotten. That is
	the defect this codebase keeps producing, so it gets a test on both transports rather
	than a comment saying the helper is used.
	"""

	private = subroutine.domain.projects.create(
		pair.session,
		workspace_id=pair.workspace.id,
		key="SECRET",
		title="Secret",
		visibility="private",
		owner_id=pair.user.id,
	)
	hidden = subroutine.domain.tasks.create(
		pair.session, project=private, title="Acquire the rival company"
	)
	pair.session.flush()

	subroutine.domain.comments.create(
		pair.session,
		entity_type="task",
		entity_id=hidden.id,
		body="Bid rejected.",
	)
	pair.session.flush()

	# Asserted here, before a second account exists: local mode resolves "the only user" by
	# counting them, so `pair.local` stops having an identity the moment the outsider is
	# created. That the narrowing is a narrowing and not a wall has to be established first.
	assert [item.body for item in pair.local.comments(ref=hidden.ref)] == ["Bid rejected."]

	outsider = subroutine.domain.users.create(
		pair.session, username=f"other-{uuid.uuid4().hex[:8]}"
	)
	subroutine.domain.workspaces.add_member(
		pair.session, pair.workspace, outsider, role_key="member"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		pair.session, user=outsider, title="outsider"
	)
	pair.session.flush()

	factory = api_support.factory_for(pair.session)
	settings = subroutine.config.Settings(dev_mode=True)
	secret = issued.value.get_secret_value()

	nosy_local = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		settings,
		session_factory=factory,
		token=secret,
	)
	nosy_remote = subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://tasks.example.com"),
		token=secret,
		transport=api_support.SyncTransport(api_support.build_app(factory)),
		base_url=api_support.BASE_URL,
	)

	with nosy_local, nosy_remote:
		for client in (nosy_local, nosy_remote):
			# Absent rather than forbidden: saying "forbidden" would confirm it exists,
			# which is the disclosure §7.3a's existence rule is written to prevent.
			assert client.task(ref=hidden.ref) is None
			assert client.document(ref=hidden.ref) is None

			with pytest.raises(subroutine.errors.NotFound):
				client.comments(ref=hidden.ref)

			with pytest.raises(subroutine.errors.NotFound):
				client.links(ref=hidden.ref)

			with pytest.raises(subroutine.errors.NotFound):
				client.remark(ref=hidden.ref, body="I should not be able to write this.")


def test_both_take_an_explicit_workspace_on_every_new_method (pair: Pair) -> None:
	"""The parameter the equivalence tests were not passing, and so could not check.

	``_given()`` on the HTTP client drops a ``None``, so a test that leaves ``workspace``
	unset never puts ``workspace_id`` on the wire — and every test above leaves it unset.
	The comments endpoints did not declare the parameter and refused it, which nothing here
	could see: the CLI always sends it, because it has resolved which workspace the ref was
	found in before it asks. It failed the first time ``subroutine show`` was pointed at a
	remote connection.

	The general lesson, which this codebase keeps relearning: **a default argument is not
	covered by a test that never overrides it.** Same shape as the page-size divergence,
	where both sides agreed until somebody passed ``limit``.
	"""

	task = make(pair, "Something to talk about")
	local, remote = pair.both()
	slug = pair.workspace.slug

	local.remark(ref=task.ref, body="Said once.", workspace=slug)

	for client in (local, remote):
		assert client.task(ref=task.ref, workspace=slug) is not None
		assert client.document(ref=task.ref, workspace=slug) is None
		assert client.links(ref=task.ref, workspace=slug) == []
		assert [item.body for item in client.comments(ref=task.ref, workspace=slug)] == [
			"Said once."
		]

	assert local.comments(ref=task.ref, workspace=slug) == remote.comments(
		ref=task.ref, workspace=slug
	)


def test_both_list_documents_the_same_way (pair: Pair) -> None:
	"""``list`` spans tasks and documents, so a document listing crossed the boundary too.

	Ordered like ``tasks`` on purpose: the CLI merges the two into one list on ``created_at``,
	and a document listing sorted by something else would interleave differently depending on
	which transport answered — the divergence §13.7 exists to prevent, in a place nobody would
	think to look.
	"""

	local, remote = pair.both()
	project = subroutine.domain.projects.create(
		pair.session, workspace_id=pair.workspace.id, key="DOCS", title="Docs"
	)

	for index in range(4):
		subroutine.domain.documents.create(
			pair.session, project=project, title=f"Finding {index}", body="Something."
		)

	pair.session.flush()

	assert local.documents() == remote.documents()
	assert [item.title for item in local.documents()] == [
		"Finding 3",
		"Finding 2",
		"Finding 1",
		"Finding 0",
	], "newest first, the same order tasks come back in"

	# The limit is one arbiter for both, the way `tasks` already is.
	assert len(local.documents(limit=2)) == len(remote.documents(limit=2)) == 2


@pytest.mark.parametrize(
	"order",
	["-priority_score", "priority_score", "-ref", "title", "-due_at,ref"],
	ids=["by-rank", "by-rank-ascending", "newest-ref", "alphabetical", "two-keys"],
)
def test_both_apply_the_same_ordering (pair: Pair, order: str) -> None:
	"""``order=`` has to mean the same thing on both sides, or a rank is transport-dependent.

	Added with the parameter itself on 2026-07-30. Until then ``clients/base.py`` took no
	ordering at all, so every listing that went through a client was newest-first while
	``GET /v1/tasks?order=`` had offered nine sort fields since S3-06 — the same divergence
	S3-07 removed for a task's *shape*, quietly recreated for its order.

	``-priority_score`` is here for a second reason: it is banded (§6.3a), and the banding is
	a rule stated twice. If a transport ever built its own ordering rather than sharing the
	domain's, this is where it would show.
	"""

	for index in range(6):
		# A spread of ranking states, so the three bands and the tiebreak are all exercised
		# rather than a page that happens to be uniform.
		suffix = ("!5/5", "!1/1", "!4", "", "!2/3", "")[index]
		make(pair, f"Task number {index} {suffix}".strip())

	local, remote = pair.both()

	assert [task.ref for task in local.tasks(order=order)] == [
		task.ref for task in remote.tasks(order=order)
	]


def test_both_refuse_an_ordering_neither_can_serve (pair: Pair) -> None:
	"""And refuse it the same way, naming the same field.

	A refusal is part of the contract: an agent that sends ``order=priority`` learns the real
	name from the message, and learning two different things depending on the transport is
	the failure this whole suite exists to prevent.
	"""

	local, remote = pair.both()

	for client in (local, remote):
		with pytest.raises(subroutine.errors.ValidationError) as raised:
			client.tasks(order="priority")

		assert raised.value.errors[0].field == "order"
		assert "priority_score" in (raised.value.errors[0].hint or "")


def test_both_apply_the_same_change (pair: Pair) -> None:
	"""``update`` has to mean the same thing on both sides, field for field.

	The one that could quietly differ is ``None``. §8.3 makes null the way to *clear* a
	field, so a transport that built its request by dropping empty values would turn "unset
	the estimate" into "change nothing" — and answer 200 either way. ``http.py``'s ``_given``
	does exactly that drop, correctly, for query strings; using it for a PATCH body is the
	mistake this test exists to catch.
	"""

	local_task = make(pair, "Local subject ~2h")
	remote_task = make(pair, "Remote subject ~2h")

	local, remote = pair.both()

	changed_here = local.update(ref=local_task.ref, importance=4, urgency=2, estimate="3h")
	changed_there = remote.update(ref=remote_task.ref, importance=4, urgency=2, estimate="3h")

	assert (changed_here.importance, changed_here.urgency) == (4, 2)
	assert changed_here.estimate_minutes == changed_there.estimate_minutes == 180

	cleared_here = local.update(ref=local_task.ref, estimate=None)
	cleared_there = remote.update(ref=remote_task.ref, estimate=None)

	assert cleared_here.estimate_minutes is None, "null did not clear locally"
	assert cleared_there.estimate_minutes is None, "null did not clear over the wire"


def test_both_refuse_a_priority_neither_can_store (pair: Pair) -> None:
	"""And name the field, on both transports.

	§6.3's range lived only in a CHECK constraint until 2026-07-30, which made this a 500 on
	one backend and silence on the other.
	"""

	task = make(pair, "Subject")
	local, remote = pair.both()

	for client in (local, remote):
		with pytest.raises(subroutine.errors.ValidationError) as raised:
			client.update(ref=task.ref, importance=9)

		assert raised.value.errors[0].field == "importance"


def test_both_search_the_same_text_the_same_way (pair: Pair) -> None:
	"""``q=`` reads title *and* description (§9.4), and must read both on both sides.

	Added to the clients on 2026-07-31 with no equivalence test, which is how three
	parameters came to sit outside this contract at once (`#116`). The description half is the
	part worth pinning: it is what `#81` fixed, and it is the half a title-only implementation
	would still pass a naive test on.
	"""

	make(pair, "Plain heading")
	described = pair.local.tasks(limit=50)[0]
	row = pair.session.get(subroutine.db.models.work.Task, described.id)

	assert row is not None

	subroutine.domain.tasks.update(
		pair.session, row, description="The keyset cursor is decoded wrongly."
	)
	pair.session.flush()
	make(pair, "Unrelated heading")

	local, remote = pair.both()
	found = sorted(task.ref for task in local.tasks(q="cursor", limit=50))

	assert found == [described.ref], "the probe matched nothing, so it proves nothing"
	assert found == sorted(task.ref for task in remote.tasks(q="cursor", limit=50))


@pytest.mark.parametrize("choice", ["include", "exclude", "only"])
def test_both_treat_deferred_work_the_same_way (pair: Pair, choice: str) -> None:
	"""All three of §6.5's deferral narrowings, because ``only`` is the one that reports.

	`subroutine list` shows the ``exclude`` set and reports the size of the ``only`` set, so a
	transport that disagreed about either would put a count beside a list that was about
	different rows — with nothing in the output saying so.
	"""

	make(pair, "Startable now")
	make(pair, "Parked from 2099-01-01")

	local, remote = pair.both()
	found = sorted(task.ref for task in local.tasks(deferred=choice, limit=50))

	assert found == sorted(task.ref for task in remote.tasks(deferred=choice, limit=50))
	assert len(found) == (2 if choice == "include" else 1), "the defer did not take"


def test_both_refuse_a_deferral_neither_understands (pair: Pair) -> None:
	"""And name the same field, so a caller learns the real values whichever it asked."""

	for client in pair.both():
		with pytest.raises(subroutine.errors.ValidationError) as raised:
			client.tasks(deferred="banana", limit=50)

		assert raised.value.errors[0].field == "deferred"
		assert "exclude" in (raised.value.errors[0].message or "")


def test_both_find_the_same_children (pair: Pair) -> None:
	"""``parent=`` narrows to one item's direct children, on both sides."""

	parent = make(pair, "The whole feature")
	project = pair.session.get(subroutine.db.models.project.Project, parent.project_id)
	row = pair.session.get(subroutine.db.models.work.Task, parent.id)

	assert project is not None and row is not None

	child = subroutine.domain.tasks.create(
		pair.session, project=project, title="A part", parent=row
	)
	pair.session.flush()

	local, remote = pair.both()
	found = [task.ref for task in local.tasks(parent=parent.ref, limit=50)]

	assert found == [child.ref]
	assert found == [task.ref for task in remote.tasks(parent=parent.ref, limit=50)]


def test_both_refuse_a_parent_that_names_nothing (pair: Pair) -> None:
	"""**Not found, not an empty list**, and the same one either way.

	An empty listing would say the subtree is empty, which is a different and false claim —
	and one that confirms the ref exists to somebody probing for it (§7.3a).
	"""

	for client in pair.both():
		with pytest.raises(subroutine.errors.NotFound):
			client.tasks(parent=9999, limit=50)


def test_both_report_the_same_changes (pair: Pair) -> None:
	"""The feed's watermark, scoping and ordering live in the domain so that this holds.

	They were in the route first, which would have made this test the only thing standing
	between an instance that withheld the last second over HTTP and one that did not withhold
	it locally — a lost event on one transport and not the other, with nothing in the output
	saying which transport you were on.
	"""

	for index in range(4):
		make(pair, f"Task number {index}")

	_settle(pair)

	local, remote = pair.both()

	assert local.changes() == remote.changes()
	assert [event.seq for event in local.changes()] == sorted(
		event.seq for event in local.changes()
	)


def test_both_name_the_item_a_change_is_about (pair: Pair) -> None:
	"""`item_ref`/`item_title` are rendered server-side precisely so both sides agree.

	Left to each client they would be two answers to one question, which is what `views.py`
	sits outside `api/` to prevent — and the CLI, an agent and any browser would each resolve
	the same ids again.
	"""

	made = make(pair, "Fix the parser")

	_settle(pair)

	local, remote = pair.both()
	named = [event for event in local.changes() if event.item_ref == made.ref]

	assert named, "the created task should be named in the feed"
	assert named[0].item_title == "Fix the parser"
	assert local.changes() == remote.changes()


def test_both_resume_from_the_same_point (pair: Pair) -> None:
	"""``since`` is inclusive on both, or a client switching transports loses or repeats a row."""

	for index in range(5):
		make(pair, f"Task number {index}")

	_settle(pair)

	local, remote = pair.both()
	everything = local.changes()
	middle = everything[2].seq

	assert local.changes(since=middle) == remote.changes(since=middle)
	assert local.changes(since=middle)[0].seq == middle


def test_both_read_the_newest_page_the_same_way (pair: Pair) -> None:
	"""``newest`` reverses the query and both sides must turn the page back the right way up."""

	for index in range(6):
		make(pair, f"Task number {index}")

	_settle(pair)

	local, remote = pair.both()
	tail = local.changes(newest=True, limit=3)

	assert tail == remote.changes(newest=True, limit=3)
	assert [event.seq for event in tail] == sorted(event.seq for event in tail)
	assert tail[-1].seq == local.changes()[-1].seq


def test_both_let_since_overrule_newest (pair: Pair) -> None:
	"""The two arguments together, which is the combination the rule is about (`#310`).

	Each was covered alone and neither test could see a divergence in the rule that decides
	between them — which is what made it safe to write ``newest and since is None`` twice, once
	per transport, and leave both.
	"""

	for index in range(6):
		make(pair, f"Task number {index}")

	_settle(pair)

	local, remote = pair.both()
	middle = local.changes()[2].seq

	# **A limit smaller than what is left, or this cannot fail.** Without one the whole
	# remainder fits on a page, both directions select the same rows, and `page` reverses the
	# backwards one into the same order — so the answer is identical whether the rule ran or
	# not. Written without the limit first, and it passed with the rule deleted.
	assert local.changes(since=middle, newest=True, limit=2) == remote.changes(
		since=middle, newest=True, limit=2
	)
	# `since` wins, so this is the page *after* the cursor rather than the tail of the feed.
	assert local.changes(since=middle, newest=True, limit=2) == local.changes(
		since=middle, limit=2
	)


def test_both_refuse_a_cursor_below_the_first_seq_the_same_way (pair: Pair) -> None:
	"""``since=0`` is a request nobody can honour, and both must say so the same way (`#309`).

	The floor was checked in the endpoint alone, so the local client fell through to the
	expiry refusal and told a caller its events had been pruned — on an instance that has
	never pruned anything, and about a cursor that was simply not a ``seq``. A refusal naming
	a cause it has not established is the failure mode this project has had to correct three
	times; here it also made the two transports disagree.
	"""

	make(pair, "Something to have happened")
	_settle(pair)

	local, remote = pair.both()

	with pytest.raises(subroutine.errors.ValidationError) as locally:
		local.changes(since=0)

	with pytest.raises(subroutine.errors.ValidationError) as remotely:
		remote.changes(since=0)

	assert str(locally.value) == str(remotely.value)
	assert "names nothing" in str(locally.value)


def test_both_count_a_project_past_a_page (pair: Pair) -> None:
	"""``#296``. A count is not a page, and `len(tasks(...))` reported the page size.

	Sixty items over a default page of fifty, because the whole defect is invisible below the
	limit — which is why `project rename` promised "50 items keep their numbers" about a
	project of 249 and nothing noticed.

	Both transports, because the local side counts over a subquery and the HTTP side asks for
	§8.4's `include_total`: two implementations of one number, which is the arrangement this
	file exists for.
	"""

	for index in range(60):
		make(pair, f"item {index}")

	local, remote = pair.both()

	assert local.count_tasks() == remote.count_tasks() == 60
	# And the listing still stops at a page, so the two answer different questions.
	assert len(local.tasks(limit=None)) < 60


def _settle (pair: Pair) -> None:
	"""Age every event past the watermark, so the feed will report it.

	The watermark is a second and this suite would otherwise sleep through it once per test.
	Shifted row by row rather than by one ``UPDATE``, because ``UtcDateTime`` normalises a
	bound value and meets the timedelta expecting a datetime.
	"""

	shift = datetime.timedelta(seconds=2)

	for event in pair.session.scalars(
		sqlalchemy.select(subroutine.db.models.activity.Event)
	):
		event.created_at = event.created_at - shift

	pair.session.flush()


def test_both_move_a_project_the_same_way (pair: Pair) -> None:
	"""`#246`. `POST /v1/projects/{key}/move` reached no client until now."""

	local, remote = pair.both()

	local.create_project(key="ACME", title="Acme")
	local.create_project(key="WEB", title="Website")
	local.create_project(key="API", title="The API", parent="WEB")

	moved = local.move_project("WEB", parent="ACME")
	acme = next(item for item in local.projects() if item.key == "ACME")

	assert moved.parent_id == acme.id
	assert local.projects() == remote.projects()

	# The subtree came with it, which is the whole of what "move" means here.
	api = next(item for item in local.projects() if item.key == "API")
	web = next(item for item in local.projects() if item.key == "WEB")

	assert api.parent_id == web.id
	assert api.depth == web.depth + 1


def test_both_take_a_project_back_to_the_root_the_same_way (pair: Pair) -> None:
	"""``parent=None`` is an instruction, not an omission — and it must survive the wire.

	`_given` drops a `None`, which is right for a filter and wrong here: the endpoint refuses
	a body naming no parent at all, precisely so that "move to root" has to be said. A client
	that dropped the key would meet that refusal instead of moving anything.
	"""

	local, remote = pair.both()

	local.create_project(key="ACME", title="Acme")
	local.create_project(key="WEB", title="Website", parent="ACME")

	assert remote.move_project("WEB", parent=None).parent_id is None
	assert local.projects() == remote.projects()


def test_both_revise_a_document_the_same_way (pair: Pair) -> None:
	"""``#291``. ``PATCH /v1/documents`` existed since M1 and no client could reach it.

	So the instance could accumulate conclusions and never correct one, which defeats the
	reason for keeping them there — §5.10 says a document is what you concluded, and one that
	cannot be revised records only what you concluded *once*. Found when a migration runbook
	changed twice within the hour it was written.
	"""

	local, remote = pair.both()

	written = local.create_document(title="What we settled", body="First thoughts.")
	revised = local.update_document(
		ref=written.ref, title="What we settled, and why", body="Second thoughts."
	)

	assert revised.title == "What we settled, and why"
	assert revised.body == "Second thoughts."
	assert revised.version > written.version

	assert local.document(ref=written.ref) == remote.document(ref=written.ref)


def test_both_clear_a_document_body_rather_than_ignoring_the_request (pair: Pair) -> None:
	"""``None`` clears and omitted is unchanged (§8.3), and the difference has to cross the wire.

	The HTTP client builds its body by comparison against ``UNSET`` rather than by dropping
	empty values for exactly this: a filter that removed nulls would answer "empty this
	document" with a 200 and no change. That is the shape `update` already carries a note
	about, and it is easier to get wrong the second time.
	"""

	local, remote = pair.both()

	written = local.create_document(title="A conclusion", body="Some reasoning.")

	# Omitted leaves it alone...
	untouched = remote.update_document(ref=written.ref, title="A conclusion, restated")

	assert untouched.body == "Some reasoning."

	# ...and null empties it, over the same transport.
	emptied = remote.update_document(ref=written.ref, body=None)

	assert emptied.body is None
	assert local.document(ref=written.ref) == remote.document(ref=written.ref)


def test_both_rename_a_workspace_the_same_way (pair: Pair) -> None:
	"""``#295``. Simon challenged the prohibition and it did not survive being checked.

	The stated reason was that a slug lives "in other people's notes, in shell history and in
	`config.toml` on other machines" — and no connection and no setting names a workspace, so
	the last of those was false. Nothing inside the database references a slug either: every
	table keys on `workspace_id`, so this moves no relationship and breaks no join.
	"""

	local, remote = pair.both()

	before = local.tasks()
	renamed = local.rename_workspace(pair.workspace.slug, slug="renamed")

	assert renamed.slug == "renamed"

	# **Every item keeps its number**, which is the whole claim the confirmation makes. A ref
	# is per workspace and the workspace is the same row — only its name moved.
	assert [task.ref for task in local.tasks(workspace="renamed")] == [
		task.ref for task in before
	]
	assert local.tasks(workspace="renamed") == remote.tasks(workspace="renamed")


def test_a_workspace_cannot_be_renamed_to_something_creation_would_refuse (pair: Pair) -> None:
	"""One validator, shared, or a rename becomes the way to get a name nobody could choose.

	`create` grew five rules over time — usable characters, length, a leading letter, not a
	reserved word, not already taken. A second copy in `update` would have started identical
	and drifted, which is this codebase's signature defect.
	"""

	local, _remote = pair.both()

	for refused in ("2026", "all", ""):
		with pytest.raises(subroutine.errors.SubroutineError):
			local.rename_workspace(pair.workspace.slug, slug=refused)

	# And renaming to the name it already has is a no-op, not a collision with itself.
	same = pair.workspace.slug

	assert local.rename_workspace(same, slug=same).slug == same


def test_both_move_a_document_to_another_project (pair: Pair) -> None:
	"""``#294``. ``project`` was accepted on create and by nothing afterwards.

	So a conclusion written before anybody had decided where it belonged stayed in the Inbox
	permanently — and unlike a task's, a document's project decides **who may read it**
	(§7.3a), which made this a permissions gap rather than an untidy one. Found when eleven
	decision documents, including a live migration runbook, could not be filed.
	"""

	local, remote = pair.both()

	local.create_project(key="DOCS", title="Docs")
	written = local.create_document(title="A conclusion", body="Reasoning.")
	moved = local.update_document(ref=written.ref, project="DOCS")

	assert moved.project_key == "DOCS"
	assert moved.ref == written.ref, "filing it somewhere else does not renumber it"

	assert local.document(ref=written.ref) == remote.document(ref=written.ref)



def test_both_include_a_sub_projects_items_under_its_parent (pair: Pair) -> None:
	"""``#320``. A named project means that area of work, not that one node.

	Every listing compared ``project_id`` to a single id, so ``project list`` drew a tree and
	``list --project PARENT`` returned only what was filed directly in it — a hierarchy whose
	parent answers for none of its contents.

	The rule already existed one function away saying the opposite: ``within_project_scope``
	narrows a *credential* by subtree and argues the case in writing. Two copies of one rule
	disagreeing, which is what this file exists to catch between transports and did not catch
	between a listing and a permission.
	"""

	parent = pair.local.create_project(key="PARENT", title="Parent")
	child = pair.local.create_project(key="CHILD", title="Child", parent=parent.key)

	assert child.parent_id == parent.id

	make(pair, "lives in the parent +PARENT")
	make(pair, "lives in the child +CHILD")

	local, remote = pair.both()
	under = local.tasks(project="PARENT")

	assert local.tasks(project="PARENT") == remote.tasks(project="PARENT")
	assert {task.title for task in under} == {
		"lives in the parent",
		"lives in the child",
	}

	# And naming the child still means the child alone — a subtree is not a widening of
	# everything, it is the branch somebody named.
	assert {task.title for task in local.tasks(project="CHILD")} == {"lives in the child"}
	assert local.count_tasks(project="PARENT") == 2
