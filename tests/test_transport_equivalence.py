"""The same scenarios through both transports, asserting the output matches.

docs/design.md §13.7 makes the local database a connection like any other so that ``subroutine
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
import subroutine.api.app
import subroutine.api.routing
import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.claims
import subroutine.domain.comments
import subroutine.domain.documents
import subroutine.domain.filtering
import subroutine.domain.links
import subroutine.domain.projects
import subroutine.domain.schedule
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


def test_a_claim_hides_the_work_from_everybody_but_its_holder (
	pair: Pair, session: sqlalchemy.orm.Session
) -> None:
	"""`#350`, and the whole point of it: two workers cannot take the same item.

	**Two principals, because one cannot fail.** A test where the claimer and the reader are
	the same credential passes whether or not the predicate distinguishes them — and telling
	them apart is the entire behaviour. Same lesson as the one-workspace fixture in `#332`.
	"""

	task = make(pair, "Something two agents would both pick up")

	# `responsible_user_id` is named explicitly rather than inherited, because this fixture has
	# no acting principal to inherit from and an agent nobody answers for is refused outright
	# (decision `#473`). The workspace's own owner is the honest answer here.
	other = subroutine.domain.users.create(
		session,
		username=f"other-{uuid.uuid4().hex[:8]}",
		is_service_account=True,
		responsible_user_id=pair.user.id,
	)
	subroutine.domain.workspaces.add_member(
		session, pair.workspace, other, role_key="contributor"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=other, title="The other worker"
	)
	session.flush()

	local, _remote = pair.both()
	theirs = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(session),
		token=issued.value.get_secret_value(),
	)

	assert task.ref in [found.ref for found in local.tasks(ready=True)], "free to start"

	local.claim(ref=task.ref)

	with theirs:
		assert task.ref not in [found.ref for found in theirs.tasks(ready=True)], (
			"another worker's live claim takes it off the list"
		)

		# **And the refusal names who, which is what the other worker does next with it.**
		with pytest.raises(subroutine.errors.Conflict) as refused:
			theirs.claim(ref=task.ref)

		assert pair.user.username in str(refused.value.hint)

	# Never hidden from its own holder: an agent that claimed something and then asked what it
	# could start would otherwise lose the thing it had just taken.
	assert task.ref in [found.ref for found in local.tasks(ready=True)]

	local.release(ref=task.ref)

	with theirs:
		assert task.ref in [found.ref for found in theirs.tasks(ready=True)], "given back"


def test_both_claim_and_release_the_same_way (pair: Pair) -> None:
	"""Whichever transport asked, the lease and what it reports are the same."""

	local, remote = pair.both()
	task = make(pair, "Claimed over one transport, released over the other")

	held = remote.claim(ref=task.ref)

	assert held.claimed_by_id == pair.user.id
	assert held.claimed_at is not None
	assert held.claim_expires_at is not None

	# Renewing keeps the instant it was first taken — how long this has been in hand is not
	# lost by saying so again.
	renewed = local.claim(ref=task.ref)

	assert renewed.claimed_at == held.claimed_at
	assert renewed.claim_expires_at is not None
	assert renewed.claim_expires_at >= held.claim_expires_at

	freed = local.release(ref=task.ref)

	assert freed.claimed_by_id is None
	assert freed.claim_expires_at is None

	# Releasing what nobody holds is not an error, so tidying up needs no check first.
	assert remote.release(ref=task.ref).claimed_by_id is None


def test_a_lease_nobody_renewed_stops_counting (
	pair: Pair, session: sqlalchemy.orm.Session
) -> None:
	"""A lease, not a lock — and nothing has to run for the work to come back.

	The case this exists for is a worker that died: no release, no cleanup job, and the task
	simply becomes available again. Asserted by putting the expiry in the past rather than by
	waiting, which is the only way to test a clock without one.
	"""

	local, _remote = pair.both()
	task = make(pair, "Taken by an agent that never came back")

	local.claim(ref=task.ref, minutes=1)

	row = session.get(subroutine.db.models.work.Task, task.id)

	assert row is not None

	row.claim_expires_at = subroutine.db.types.utcnow() - datetime.timedelta(minutes=1)
	session.flush()

	assert subroutine.domain.claims.held_by(row) is None, "the row-level reading"
	assert task.ref in [
		found.ref for found in local.tasks(ready=True)
	], "and the predicate the database sorts by, which has to agree with it"


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

	from_local = local.capture(text="Water the plants every fortnight")
	from_remote = remote.capture(text="Water the plants every fortnight")

	assert from_local.unparsed == from_remote.unparsed == ("every fortnight",)

	# §6.13 rule 1: nothing is lost, so the words stay in the title on both paths.
	assert (
		from_local.task.title == from_remote.task.title == "Water the plants every fortnight"
	)

	# **And what the grammar *does* read has to agree too** (`#94`). The HTTP client parses the
	# line a second time locally to produce its advisory sentence, so the two ends can disagree
	# about one line — which is the assumption `/v1/meta` publishes the grammar to make
	# checkable, and this is the cheap half of that check.
	read_local = local.capture(text="Pay the rent on the 30th of every month")
	read_remote = remote.capture(text="Pay the rent on the 30th of every month")

	assert read_local.unparsed == read_remote.unparsed == ()
	assert read_local.task.title == read_remote.task.title == "Pay the rent"
	assert (
		read_local.task.recurrence_rule
		== read_remote.task.recurrence_rule
		== "FREQ=MONTHLY;BYMONTHDAY=30"
	)


def test_both_report_which_work_is_blocked (pair: Pair) -> None:
	"""Item ``#425``. The listing said nothing, so a blocked item sorted above its blocker.

	``?ready=true`` has excluded blocked work since `#69` — but a *filter* is not a signal, and
	the default listing is the one somebody reads. An agent on a fresh install reported it as
	"start with #2", and `#69` itself had recorded the same observation about `#57` and `#58`
	before shipping only the filter half.

	Both transports, because the field is loaded per page by ``Vocabulary`` — one query for the
	whole page (`#39`'s N+1 was the recorded obstacle) — and a marker that appeared on one
	transport and not the other would be worse than none.
	"""

	local, remote = pair.both()

	blocker = local.capture(text="Fix the thing that blocks the other").task
	blocked = local.capture(text="The thing that is blocked").task

	local.link(ref=blocker.ref, link_type="blocks", target=blocked.ref)

	def seen (client: subroutine.clients.base.Client) -> dict[int, bool]:
		"""Return which refs each transport reports as blocked."""

		return {row.ref: row.blocked for row in client.tasks(limit=50)}

	assert seen(local) == seen(remote)
	assert seen(local)[blocked.ref] is True
	assert seen(local)[blocker.ref] is False

	# **It follows the links rather than a stored flag**, which is why it is not writable:
	# finishing the blocker changes it without anybody touching the blocked item.
	local.complete(ref=blocker.ref)

	assert seen(local)[blocked.ref] is False
	assert seen(remote)[blocked.ref] is False


def test_both_carry_a_description_beside_the_captured_line (pair: Pair) -> None:
	"""Item ``#424``. The endpoint took this beside ``text`` since M1; neither client did.

	**A capability on a route, missing as an argument on a method both surfaces already call.**
	``test_reach`` compares method *names*, so a gap of this shape is invisible to it — the
	fourth time (`#178`, `#367`, `#392`), and every one was found by somebody using the
	product rather than by the suite. This one by an agent asked why the six items it had just
	filed had no descriptions; it checked, found the option genuinely absent, and said the
	skill's argument for outcome-shaped titles depends on a field it could not reach.

	Both transports, because the local client passes it as an override into
	``create_from_text`` and the remote one puts it in the body — two mechanisms for one
	promise, which is what this file exists to hold together.
	"""

	local, remote = pair.both()
	reasoning = "Measured at 400ms a call, four calls a listing."

	from_local = local.capture(text="Cache the roster !3/2", description=reasoning)
	from_remote = remote.capture(text="Cache the roster !3/2", description=reasoning)

	assert from_local.task.description == from_remote.task.description == reasoning

	# The line is still parsed as it always was: a description is beside the grammar, never
	# part of it, so nothing about the sentence changes by supplying one.
	assert from_local.task.title == from_remote.task.title == "Cache the roster"
	assert from_local.task.importance == from_remote.task.importance == 3

	# And saying nothing still means nothing — not an empty description, which is a different
	# claim and would be a caller overriding a field they never mentioned.
	assert local.capture(text="Buy milk").task.description is None
	assert remote.capture(text="Buy milk").task.description is None


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

	by_local = local.create_project(key="alpha", title="From the local client")
	by_remote = remote.create_project(key="beta", title="From the HTTP client")

	assert by_local.key == "alpha"
	assert by_remote.key == "beta"

	# Every field that is not the two they were told to differ in. A create is where a default
	# most easily comes to be decided in two places — `visibility` is passed explicitly by the
	# HTTP client for exactly that reason.
	# `path` differs because `key` does — it is the address `key` is the last segment of
	# (`#512`), so the two projects being compared could not share one.
	differs = {"id", "key", "path", "title", "created_at", "updated_at"}
	as_local = by_local.model_dump()
	as_remote = by_remote.model_dump()

	assert {name: value for name, value in as_local.items() if name not in differs} == {
		name: value for name, value in as_remote.items() if name not in differs
	}


def test_both_list_the_same_projects_parents_before_children (pair: Pair) -> None:
	"""Ordered by path, so the tree prints in one pass without the caller reassembling it."""

	local, remote = pair.both()

	above = local.create_project(key="outer", title="Outer")
	local.create_project(key="inner", title="Inner", parent=above.key)

	from_local = [(one.key, one.depth) for one in local.projects()]
	from_remote = [(one.key, one.depth) for one in remote.projects()]

	assert from_local == from_remote
	assert from_local.index(("outer", 0)) < from_local.index(("inner", 1))


def test_both_sort_a_project_listing_by_the_same_vocabulary (pair: Pair) -> None:
	"""`#501`'s last filter, and the only one that needed a move rather than an argument.

	The sort vocabulary lived in ``api/projects.py``, where one transport could see it — so
	``GET /v1/projects`` accepted an ``?order=`` no client could ask for and nothing could
	compare the two. Moved beside ``TASK_FIELDS`` and ``DOCUMENT_FIELDS``, which is where the
	other two listings had it and why only projects were unreachable.

	Sorted by ``-key`` rather than by ``path``: the default *is* path, so ordering by it would
	pass against a client that ignored the argument entirely.
	"""

	local, remote = pair.both()

	above = local.create_project(key="mid", title="Middle")
	local.create_project(key="alpha", title="Alpha", parent=above.key)
	local.create_project(key="zulu", title="Zulu")

	from_local = [one.key for one in local.projects(order="-key")]
	from_remote = [one.key for one in remote.projects(order="-key")]

	assert from_local == from_remote
	assert from_local.index("zulu") < from_local.index("alpha"), "not sorted by key at all"

	# And the tree order is still what you get for asking nothing, which is the property the
	# move had to preserve — `DEFAULT_PROJECT_ORDER` is now the only place that says so.
	assert [one.key for one in local.projects()].index("mid") < (
		[one.key for one in local.projects()].index("alpha")
	)


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

	by_local = local.create_project(key="hush", title="Quiet", visibility="private")
	by_remote = remote.create_project(key="shh", title="Quieter", visibility="private")

	assert by_local.owner_id == by_remote.owner_id == pair.user.id
	assert {one.key for one in local.projects()} >= {"hush", "shh"}
	assert {one.key for one in remote.projects()} >= {"hush", "shh"}


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

	local.schedule(ref=later.ref, snooze=datetime.date.today() + datetime.timedelta(days=7))

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


def test_both_skip_an_occurrence_the_same_way (pair: Pair) -> None:
	"""`#94`. And both refuse to skip something that is not one of a series.

	**Added because this file is hand-listed**, which is how `#44`'s ``move`` went uncovered:
	a new verb reaches both clients and nothing here notices unless somebody writes the case.
	The failure it would hide is the worse half — a `skip` that cancelled on one transport and
	completed on the other reads as done work on one surface and abandoned work on the other.
	"""

	first = pair.local.capture(
		text="Water the plants by 2026-12-01", recurrence="every 14 days"
	).task
	second = pair.local.capture(
		text="And these ones by 2026-12-01", recurrence="every 14 days"
	).task

	local, remote = pair.both()

	by_local = local.skip(ref=first.ref)
	by_remote = remote.skip(ref=second.ref)

	assert by_local.completed_at is not None
	assert by_remote.completed_at is not None
	assert by_local.status_category == by_remote.status_category == "cancelled"

	# **Refused identically**, which is the half an equivalence suite is really for: a message
	# that differs by transport is a client learning two different products.
	plain = make(pair, "This one happens once")

	for client in (local, remote):
		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			client.skip(ref=plain.ref)

		assert "repeating series" in str(refused.value)


def test_both_expand_a_repeat_into_the_same_dates (pair: Pair) -> None:
	"""`SR#94`, §6.7 — and this file is hand-listed, which is how `SR#44`'s ``move`` went uncovered.

	The two implementations are genuinely separate here: the HTTP client asks the endpoint and
	the local one computes it, so *the same rule* is being applied twice rather than once
	behind a shared call. A drift would be a calendar that disagrees with the API about which
	Tuesday, which is the worst available failure — plausible on both sides and wrong on one.
	"""

	first = pair.local.capture(
		text="Water the plants by 2026-12-01", recurrence="every 14 days"
	).task
	second = pair.local.capture(
		text="And these ones by 2026-12-01", recurrence="every 14 days"
	).task

	local, remote = pair.both()

	by_local = local.occurrences(ref=first.ref, limit=4)
	by_remote = remote.occurrences(ref=second.ref, limit=4)

	assert by_local.occurrences == by_remote.occurrences
	assert by_local.rule == by_remote.rule == "FREQ=DAILY;INTERVAL=14"
	assert by_local.description == by_remote.description
	assert by_local.has_more == by_remote.has_more is True

	# Refused identically where there is no repeat to expand — an empty list would read as
	# *it never comes round*, which is a plausible, complete, wrong answer on both transports.
	plain = make(pair, "This one happens once")

	for client in (local, remote):
		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			client.occurrences(ref=plain.ref)

		assert "does not repeat" in str(refused.value)


#: Every call ``subroutine show`` makes, by name. **Four rather than one, and that is the whole
#: point of this guard**: `#700` and `#921` were both a ref that ``task()`` resolved on both
#: transports and a *sub-resource* refused on one — so a check that asked only for the item
#: would have passed through both defects. `#700`'s own record says it: "the divergence was not
#: in what either client *returns* … but in what one of them **asks for** one layer down".
_Asked = typing.Callable[
	[subroutine.clients.local.Client | subroutine.clients.http.Client, int], typing.Any
]

_WHAT_SHOW_ASKS_FOR: tuple[_Asked, ...] = (
	lambda client, ref: client.task(ref=ref),
	lambda client, ref: client.links(ref=ref),
	lambda client, ref: client.comments(ref=ref),
	lambda client, ref: client.tasks(parent=ref),
)


def test_a_ref_this_product_publishes_resolves_on_both_transports (pair: Pair) -> None:
	"""`#921`, and `#700` before it: a number we hand back must name something.

	**Derived from the view rather than listed**, so a field added tomorrow that carries a ref
	is covered without anybody remembering this test. Today that is ``parent_ref`` and
	``recurrence_template_ref``; the rule is about the shape, not about those two.

	`#921` was the second time a published ref resolved over HTTP and not locally, and it took
	**five** lookups to close where the item named one — ``_row``, ``_subject``, the children
	query, ``domain.comments`` and the template flag itself. Each refused in different words, so
	fixing one moved the message rather than removing it and read like progress. That is why
	this drives every call ``show`` makes instead of asserting on one.

	The occurrence is what a client actually holds — nothing lists a template (§6.7) — so the
	ref under test is reached the way a caller reaches it: off a row it was given.
	"""

	series = pair.local.capture(
		text="Water the plants by 2026-12-01", recurrence="every 14 days"
	).task

	local, remote = pair.both()
	occurrence = local.task(ref=series.ref)

	assert occurrence is not None

	published = {
		name: getattr(occurrence, name)
		for name in type(occurrence).model_fields
		if name.endswith("_ref") and getattr(occurrence, name) is not None
	}

	# The fixture has to produce at least one, or this passes by having nothing to check —
	# which is the shape that let `#921` ship in the first place.
	assert "recurrence_template_ref" in published

	for ref in published.values():
		for asked in _WHAT_SHOW_ASKS_FOR:
			asked(local, ref)
			asked(remote, ref)

	# **And it is legible once it resolves**, which is the other half: a series and its
	# occurrence carry the same title, so a ref that resolves to something indistinguishable
	# from the row you started on has answered without informing.
	template = local.task(ref=published["recurrence_template_ref"])

	assert template is not None
	assert template.is_template is True
	assert remote.task(ref=published["recurrence_template_ref"]) == template

	# **A listing still hides it**, and that is not in tension with the above — a rule is not
	# work (§6.7). Reading what a ref names and listing it as something to do are different
	# questions, and this guard would otherwise be satisfied by simply publishing templates.
	for client in (local, remote):
		assert all(row.ref != template.ref for row in client.tasks())


def test_both_change_how_a_repeat_is_measured_without_re_sending_the_rule (
	pair: Pair,
) -> None:
	"""`#918`, driven over both transports because it is a whole-wire claim.

	The domain tests prove the rule; this proves the value survives the journey. Every one of
	this arc's three silent-discard defects was a field that a signature accepted and a body
	dropped somewhere between the client and the column, so the check that matters is *send
	it, read it back, and see that it moved*.
	"""

	first = pair.local.capture(
		text="Water the plants by 2026-12-01", recurrence="every 3 days"
	).task
	second = pair.local.capture(
		text="And these ones by 2026-12-01", recurrence="every 3 days"
	).task

	local, remote = pair.both()

	assert first.recurrence_anchor == "schedule", "the default these move off"

	by_local = local.update(ref=first.ref, recurrence_anchor="completion")
	by_remote = remote.update(ref=second.ref, recurrence_anchor="completion")

	assert by_local.recurrence_anchor == by_remote.recurrence_anchor == "completion"

	# **And the rule it qualifies is untouched on both**, which is what a caller was
	# previously forced to re-send in order to change the field beside it.
	assert by_local.recurrence_rule == by_remote.recurrence_rule == "FREQ=DAILY;INTERVAL=3"

	# Refused identically where there is no repeat to qualify, for the reason the skip test
	# above gives: a message that differs by transport is a client learning two products.
	plain = make(pair, "This one happens once")

	for client in (local, remote):
		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			client.update(ref=plain.ref, recurrence_anchor="completion")

		assert "does not repeat" in str(refused.value)


def test_both_schedule_a_task_the_same_way (pair: Pair) -> None:
	"""And both tell an omitted field from a null one (§8.3)."""

	first = make(pair, "Plan this one")
	second = make(pair, "And this one")
	day = datetime.date(2026, 8, 3)

	local, remote = pair.both()

	# **A day is stored as its first instant now** (`#854`), so the round trip is not the
	# identity it was when this column held a bare date. Compared as the day it falls on where
	# the task lives, which is what the caller asked for.
	def began (task: subroutine.views.Task) -> datetime.date | None:
		"""Return the day a task starts on, where it was written."""

		return None if task.starts_at is None else subroutine.domain.schedule.local_date(
			task.starts_at, task.timezone or "UTC"
		)

	assert began(local.schedule(ref=first.ref, starts=day)) == day
	assert began(remote.schedule(ref=second.ref, starts=day)) == day

	# Setting the other field must leave the first alone — the difference between "not
	# mentioned" and "cleared", which is the bug §8.3 exists to prevent.
	assert began(local.schedule(ref=first.ref, snooze=day)) == day
	assert began(remote.schedule(ref=second.ref, snooze=day)) == day

	assert local.schedule(ref=first.ref, starts=None).starts_at is None
	assert remote.schedule(ref=second.ref, starts=None).starts_at is None


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
			lambda: client.schedule(ref=1, starts=datetime.date(2026, 8, 3)),
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
	"""The new read surface, narrowed the same way everything else is (docs/design.md §7.3).

	``show`` gave the clients three new ways into the database — a document by ref, an item's
	links and its record of what happened — and each is a point lookup by ref, which is the
	shape most likely to be written as a direct query with the narrowing forgotten. That is
	the defect this codebase keeps producing, so it gets a test on both transports rather
	than a comment saying the helper is used.
	"""

	private = subroutine.domain.projects.create(
		pair.session,
		workspace_id=pair.workspace.id,
		key="secret",
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
		pair.session, workspace_id=pair.workspace.id, key="docs", title="Docs"
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
	[
		"-priority_score", "priority_score", "-ref", "title", "-due_at,ref",
		# **`SR#877`, and it is the sharpest case this parametrisation has.** `deferred` is
		# not in `TASK_FIELDS`: both sides add it per request, because the band it sorts by
		# depends on the clock. So a transport that built the vocabulary its own way would
		# refuse this outright on one side and serve it on the other — which is the very
		# divergence the test above measures for a name they *both* declare.
		"deferred,-created_at",
	],
	ids=[
		"by-rank", "by-rank-ascending", "newest-ref", "alphabetical", "two-keys",
		"deferred-last",
	],
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

	made = []

	for index in range(6):
		# A spread of ranking states, so the three bands and the tiebreak are all exercised
		# rather than a page that happens to be uniform.
		suffix = ("!5/5", "!1/1", "!4", "", "!2/3", "")[index]
		# **Two rows in the far band** (`SR#877`), so `deferred` has a boundary to place rather
		# than a page that is uniform and cannot disagree with itself. A year out, derived, so
		# the fixture cannot expire.
		#
		# **`from`, not `at`** — the first version of this used `at <date>`, which the capture
		# grammar does not read as a date at all: it stayed in the title, nothing was deferred,
		# and the case passed by having nothing to sort. Asserted below rather than trusted.
		parked = " from " + (datetime.date.today() + datetime.timedelta(days=365)).isoformat()
		made.append(
			make(pair, f"Task number {index} {suffix}{parked if index in (1, 4) else ''}".strip())
		)

	assert sum(1 for task in made if task.snoozed_until is not None) == 2, (
		"the seed deferred nothing, so `deferred` has one band and cannot disagree with itself"
	)

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

	# **`#620`: every word, in any order, across both fields.** Two words that are neither
	# adjacent nor in the source's order, one of them from the title and one from the
	# description — the case that used to return nothing, checked on both transports because
	# there are two call sites and one of them is the local client's own query.
	spread = sorted(task.ref for task in local.tasks(q="cursor heading", limit=50))

	assert spread == [described.ref], "the probe matched nothing, so it proves nothing"
	assert spread == sorted(task.ref for task in remote.tasks(q="cursor heading", limit=50))


def test_both_find_an_item_by_its_own_number (pair: Pair) -> None:
	"""**`#867`, checked here because there are two call sites and one is not the endpoint.**

	``clients/local.py`` builds its own copy of this query rather than going through
	``GET /v1/tasks``, so a predicate added to the endpoint alone reaches HTTP and silently
	not the terminal — which is the divergence `#700` found in a lookup and `#583` found in a
	rendering. The CLI and MCP both arrive through the local client.
	"""

	subject = make(pair, "Wholly unlike the query")
	make(pair, "Another item entirely")

	local, remote = pair.both()
	found = sorted(task.ref for task in local.tasks(q=str(subject.ref), limit=50))

	assert found == [subject.ref], "the probe matched nothing, so it proves nothing"
	assert found == sorted(task.ref for task in remote.tasks(q=str(subject.ref), limit=50))


def test_both_read_comments_when_they_search (pair: Pair) -> None:
	"""**`#83` on both transports, because the local client builds its own query.**

	The same reason `#867` needed one: `clients/local.py` does not go through
	``GET /v1/tasks``, so a predicate added to the endpoint alone reaches HTTP and silently not
	the terminal — and the terminal is where ``subroutine search`` and every MCP call arrive.
	"""

	subject = make(pair, "An unremarkable heading")
	make(pair, "Another unremarkable heading")

	pair.local.remark(ref=subject.ref, body="The planner turns this into a semi-join.")

	local, remote = pair.both()
	found = sorted(task.ref for task in local.tasks(q="semi-join", limit=50))

	assert found == [subject.ref], "the probe matched nothing, so it proves nothing"
	assert found == sorted(task.ref for task in remote.tasks(q="semi-join", limit=50))


def test_both_find_a_finished_item_by_its_number (pair: Pair) -> None:
	"""`#873` on both transports, because `completion_wanted` is called from two places.

	Its own docstring says why it lives in the domain: *a rule applied on one side would make
	``status_category="done"`` return the finished work over HTTP and an empty list locally.*
	The same holds for a lookup, and the local client is where ``subroutine search`` and every
	MCP call arrive.
	"""

	subject = make(pair, "Long since dealt with")
	row = pair.session.get(subroutine.db.models.work.Task, subject.id)

	assert row is not None

	subroutine.domain.tasks.complete(pair.session, row, actor=None)
	pair.session.flush()

	local, remote = pair.both()
	found = sorted(task.ref for task in local.tasks(q=str(subject.ref), limit=50))

	assert found == [subject.ref], "the probe matched nothing, so it proves nothing"
	assert found == sorted(task.ref for task in remote.tasks(q=str(subject.ref), limit=50))


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

	local.create_project(key="acme", title="Acme")
	local.create_project(key="web", title="Website")
	local.create_project(key="api", title="The API", parent="web")

	moved = local.move_project("web", parent="acme")
	acme = next(item for item in local.projects() if item.key == "acme")

	assert moved.parent_id == acme.id
	assert local.projects() == remote.projects()

	# The subtree came with it, which is the whole of what "move" means here.
	api = next(item for item in local.projects() if item.key == "api")
	web = next(item for item in local.projects() if item.key == "web")

	assert api.parent_id == web.id
	assert api.depth == web.depth + 1


def test_both_take_a_project_back_to_the_root_the_same_way (pair: Pair) -> None:
	"""``parent=None`` is an instruction, not an omission — and it must survive the wire.

	`_given` drops a `None`, which is right for a filter and wrong here: the endpoint refuses
	a body naming no parent at all, precisely so that "move to root" has to be said. A client
	that dropped the key would meet that refusal instead of moving anything.
	"""

	local, remote = pair.both()

	local.create_project(key="acme", title="Acme")
	local.create_project(key="web", title="Website", parent="acme")

	assert remote.move_project("web", parent=None).parent_id is None
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

	local.create_project(key="docs", title="Docs")
	written = local.create_document(title="A conclusion", body="Reasoning.")
	moved = local.update_document(ref=written.ref, project="docs")

	assert moved.project_key == "docs"
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

	parent = pair.local.create_project(key="parent", title="Parent")
	child = pair.local.create_project(key="child", title="Child", parent=parent.key)

	assert child.parent_id == parent.id

	make(pair, "lives in the parent +parent")
	make(pair, "lives in the child +child")

	local, remote = pair.both()
	under = local.tasks(project="parent")

	assert local.tasks(project="parent") == remote.tasks(project="parent")
	assert {task.title for task in under} == {
		"lives in the parent",
		"lives in the child",
	}

	# And naming the child still means the child alone — a subtree is not a widening of
	# everything, it is the branch somebody named.
	assert {task.title for task in local.tasks(project="child")} == {"lives in the child"}
	assert local.count_tasks(project="parent") == 2


def test_both_withdraw_a_comment_the_same_way (pair: Pair) -> None:
	"""Item ``#400``, and the reason the HTTP client does a lookup before its delete.

	``DELETE /v1/comments/{id}`` addresses a comment by its own id, so unlike ``unlink`` —
	whose ref is in the path — the route cannot refuse a caller that named the wrong item.
	The local client narrows in SQL. Without the matching lookup over HTTP the two transports
	would enforce different things, and a caller could delete across items on one of them.
	"""

	local, remote = pair.both()
	one = make(pair, "Commented on over both transports")
	other = make(pair, "Somebody else's item")

	written = local.remark(ref=one.ref, body="what happened here")
	elsewhere = local.remark(ref=other.ref, body="what happened there")

	# Naming the wrong item is refused, identically, on both.
	for client in (local, remote):
		with pytest.raises(subroutine.errors.NotFound):
			client.uncomment(ref=one.ref, comment_id=str(elsewhere.id))

	assert [one.body for one in remote.comments(ref=other.ref)] == ["what happened there"]

	remote.uncomment(ref=one.ref, comment_id=str(written.id))

	assert local.comments(ref=one.ref) == []
	assert remote.comments(ref=one.ref) == []

	# And withdrawing what is already gone is refused rather than silently succeeding, because
	# the id no longer resolves to a live comment on that item.
	for client in (local, remote):
		with pytest.raises(subroutine.errors.NotFound):
			client.uncomment(ref=one.ref, comment_id=str(written.id))


def test_both_transports_report_the_same_vocabulary (session: sqlalchemy.orm.Session) -> None:
	"""`#486`. The whole reason the models left ``api/`` for ``views``.

	`#483` declined to publish ``/v1/meta`` over MCP because the local client would have had to
	*rebuild* what the endpoint assembles — and a second implementation of "what does this
	installation call things" is the divergence S3-07 removed for tasks, aimed at the one
	response whose entire job is telling a caller what it may send.

	So there is one assembly and this is what says so. Comparing the whole document rather than
	a field or two, because the failure being guarded against is a *section* answered differently
	— which is exactly what a hand-written local version would have got wrong first.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Theirs"
	)
	session.flush()

	factory = api_support.factory_for(session)
	secret = issued.value.get_secret_value()

	local = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=factory,
		token=secret,
	)
	remote = subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://example.com"),
		token=secret,
		transport=api_support.SyncTransport(api_support.build_app(factory)),
		base_url=api_support.BASE_URL,
	)

	with local:
		here = local.meta()

	with remote:
		there = remote.meta()

	# `server_time` moves between the two calls and `public_url` is the deployment's, not the
	# document's. Everything else is a claim about this installation and must match exactly.
	moving = {"server_time"}
	locally = here.model_dump(exclude=moving)
	remotely = there.model_dump(exclude=moving)

	assert locally == remotely

	assert locally["statuses"], "both agreed on nothing, which would pass and prove nothing"
	assert locally["listings"]["task"]["filters"]


@pytest.mark.parametrize("transport", ["local", "remote"])
def test_a_read_only_connection_refuses_a_raw_write_too (
	session: sqlalchemy.orm.Session, transport: str
) -> None:
	"""`#485`. The escape hatch may not be an escape from *this*.

	**Found by falsification, not by design.** Removing the guard from ``call_api`` on both
	clients failed nothing: the test above walks ``capture``, ``update`` and ``complete``, and a
	method added later is invisible to it — the same shape as the defect it was itself written
	for, one surface along.

	§13.7 calls ``read_only`` a *client-side* control precisely because an employer's server
	cannot be asked to arrange it on the agent-owner's behalf. A raw call that skipped it would
	therefore not be a smaller hole than the original; it would be the whole feature missing,
	reachable by anything that can spell a path.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Read only"
	)
	session.flush()

	factory = api_support.factory_for(session)
	secret = issued.value.get_secret_value()
	client: subroutine.clients.base.Client

	if transport == "local":
		client = subroutine.clients.local.Client(
			subroutine.connections.Connection(name="local", read_only=True),
			subroutine.config.Settings(dev_mode=True),
			session_factory=factory,
			token=secret,
		)

	else:
		client = subroutine.clients.http.Client(
			subroutine.connections.Connection(
				name="work", url="https://employer.example.com", read_only=True
			),
			token=secret,
			transport=api_support.SyncTransport(api_support.build_app(factory)),
			base_url=api_support.BASE_URL,
		)

	with client:
		for method, path in (
			("POST", "/v1/tasks"),
			("PATCH", "/v1/tasks/1"),
			("DELETE", "/v1/tasks/1"),
		):
			with pytest.raises(subroutine.errors.SubroutineError) as refused:
				client.call_api(method=method, path=path, body={"title": "Nope"})

			assert "read" in str(refused.value).lower(), (
				f"{method} {path} was refused for the wrong reason: {refused.value}"
			)

		# And a read still works, so the rule is `read_only` rather than `no_api`.
		assert client.call_api(method="GET", path="/v1/tasks").status == 200


@pytest.mark.parametrize("transport", ["local", "remote"])
def test_neither_transport_will_aim_the_credential_at_another_host (
	pair: Pair, transport: str
) -> None:
	"""`#529`. httpx treats an absolute URL as a *replacement* for the base URL, not as a path.

	The client's default headers go with it, and one of them is ``Authorization: Bearer …`` —
	so a raw call given a whole URL sends this connection's credential to whoever asked for it.
	Measured before it was fixed, against a real `build_request`.

	It was unreachable at the time, because ``mcp/tools`` refuses a path that does not start with
	``/`` and was the only caller. **That is the finding rather than the mitigation**: the guard
	sat a layer above the credential it protects, so the second caller would have inherited a
	way to leak a token without anybody deciding to grant one.

	``//host/x`` is refused too. httpx merges it against the base URL's host today, so it is
	currently harmless — and letting it through would be relying on one library's merge rule to
	stay put, on the one argument that decides where a credential goes.
	"""

	local, remote = pair.both()
	client = local if transport == "local" else remote

	for path in ("https://elsewhere.example/collect", "//elsewhere.example/x", "not-a-path"):
		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			client.call_api(method="GET", path=path)

		assert "not a route" in str(refused.value), (
			f"{path!r} was refused for the wrong reason on {transport}: {refused.value}"
		)

	# And an ordinary path still works, so the rule is "a route" rather than "no raw calls".
	assert client.call_api(method="GET", path="/v1/tasks").status == 200


# --- Listing filters, which nothing could pass until `#501` ------------------------------


def test_both_narrow_to_one_assignee_by_username (pair: Pair) -> None:
	"""`#501`. *"What is assigned to whom"* — the question `#473`'s delegation model is for.

	`GET /v1/tasks?assignee_id=` has existed since M1 and no client passed it, so this was
	reachable only by an agent holding a **UUID**. It takes a username now, resolved by the
	service so that one refusal covers both transports, and this is what says the two of them
	resolve the same name to the same rows.
	"""

	mine = make(pair, f"Mine to do @{pair.user.username}")
	nobodys = make(pair, "Unassigned on purpose")

	local, remote = pair.both()
	here = {task.ref for task in local.tasks(assignee=pair.user.username)}

	assert here == {task.ref for task in remote.tasks(assignee=pair.user.username)}
	assert mine.ref in here
	assert nobodys.ref not in here, "an unassigned task belongs to nobody, not to everybody"


def test_both_refuse_an_assignee_who_is_not_here_the_same_way (pair: Pair) -> None:
	"""`#501`. **The refusal is the half that matters**, which is why it is resolved once.

	Filtering by a name nobody has is a typo, and the alternative — an empty list — reads
	exactly like a person who happens to have nothing on. Both transports must say so, and
	must say it the same way, or a client fanning out gets two vocabularies of failure.
	"""

	local, remote = pair.both()

	for client in (local, remote):
		with pytest.raises(subroutine.errors.NotFound) as refused:
			client.tasks(assignee="nobody-by-that-name")

		assert "nobody-by-that-name" in str(refused.value)


def test_both_narrow_to_one_status_and_one_type (pair: Pair) -> None:
	"""`#501`. The everyday pair: an agent triaging cannot ask for open bugs without them.

	Both are **per-workspace vocabulary** (§5.5), so each side resolves the key through the
	domain rather than comparing a string to a column — an unknown one is refused by name on
	both, rather than matching nothing on one and raising on the other.
	"""

	bug = make(pair, "Something is broken")
	local, remote = pair.both()

	local.update(ref=bug.ref, type="bug")
	ordinary = make(pair, "Something ordinary")

	bugs = {task.ref for task in local.tasks(type="bug")}

	assert bugs == {task.ref for task in remote.tasks(type="bug")}
	assert bugs == {bug.ref}, "the ordinary task is not a bug"

	open_now = {task.ref for task in local.tasks(status="open")}

	assert open_now == {task.ref for task in remote.tasks(status="open")}
	assert {bug.ref, ordinary.ref} <= open_now


def test_both_read_a_whole_subtree_rather_than_one_generation (pair: Pair) -> None:
	"""`#501`. A delegated piece of work broken up is a tree, and its children are not it.

	`parent` alone answers "what did I split this into"; `subtree` answers "how is it going",
	which is the question somebody who handed the work over is actually asking.
	"""

	local, remote = pair.both()

	top = make(pair, "The whole job")

	# Built through the domain because **no client can file a task under another one** —
	# `#510`, found by writing this test and having nothing that could make a subtree.
	above = pair.session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.ref == top.ref,
			subroutine.db.models.work.Task.workspace_id == pair.workspace.id,
		)
	).one()
	inbox = pair.session.get(
		subroutine.db.models.project.Project, above.project_id
	)
	assert inbox is not None

	middle = subroutine.domain.tasks.create(
		pair.session, project=inbox, title="A part of it", parent=above, actor=None
	)
	bottom = subroutine.domain.tasks.create(
		pair.session, project=inbox, title="A part of the part", parent=middle, actor=None
	)
	pair.session.flush()

	direct = {task.ref for task in local.tasks(parent=top.ref)}
	whole = {task.ref for task in local.tasks(parent=top.ref, subtree=True)}

	assert direct == {task.ref for task in remote.tasks(parent=top.ref)}
	assert whole == {task.ref for task in remote.tasks(parent=top.ref, subtree=True)}

	assert direct == {middle.ref}, "one generation"
	assert whole == {middle.ref, bottom.ref}, "the tree, and never the parent itself"


def test_both_refuse_a_subtree_with_no_parent_to_be_under (pair: Pair) -> None:
	"""`#501`. `subtree` is a widening of `parent`, so alone it is a caller who meant something.

	Ignoring it would answer a different question than the one asked and look like an answer,
	which is `#379`'s rule — an unrecognised argument is refused rather than swallowed.
	"""

	local, remote = pair.both()

	for client in (local, remote):
		with pytest.raises(subroutine.errors.ValidationError) as refused:
			client.tasks(subtree=True)

		assert "parent" in str(refused.value)


def test_both_narrow_documents_to_the_ones_still_in_force (pair: Pair) -> None:
	"""`#501`, and what `#506` needs. §6.14's lifecycle was unreachable from any client.

	A document is *draft*, then *active*, then *superseded*. Asking a workspace for its
	**active decisions** is how a reader finds the rules it is working under without being
	told each one by name — and until this, nothing but raw HTTP could ask.
	"""

	local, remote = pair.both()

	settled = local.create_document(title="A decision taken", type="decision")
	unrelated = local.create_document(title="A note", type="note")

	# **Said explicitly, because a decision is now in force the moment it is written** (`#506`).
	# This test was written before that and passed by accident: both decisions were drafts, so
	# marking one active was what separated them. Somebody drafting a decision they have not
	# taken has to be able to say so, and that is what this row is here to prove.
	drafting = local.create_document(
		title="Still being written", type="decision", status="draft"
	)

	in_force = {found.ref for found in local.documents(type="decision", status="active")}

	assert in_force == {
		found.ref for found in remote.documents(type="decision", status="active")
	}
	assert in_force == {settled.ref}
	assert drafting.ref not in in_force and unrelated.ref not in in_force


def test_both_narrow_projects_the_same_way (pair: Pair) -> None:
	"""`#501`. `parent`, `visibility` and `include_archived` reached no client either."""

	local, remote = pair.both()

	top = local.create_project(key="top", title="A tree")
	local.create_project(key="under", title="Beneath it", parent=top.key)
	local.create_project(key="hidden", title="Not for everyone", visibility="private")

	children = {project.key for project in local.projects(parent=top.key)}

	assert children == {project.key for project in remote.projects(parent=top.key)}
	assert children == {"under"}

	private = {project.key for project in local.projects(visibility="private")}

	assert private == {project.key for project in remote.projects(visibility="private")}
	assert private == {"hidden"}


def test_both_hand_a_task_over_after_it_was_filed (pair: Pair) -> None:
	"""`#493`. **The delegation move, and no surface could make it.**

	`@name` in a captured line has always worked, so a task could be assigned *when it was
	filed* and never afterwards — which means work could not be passed between two people or
	two agents once it was under way. That is the whole of decision `#473`'s model, unusable.

	`assigned_by_id` moves with it, because who handed it over is the question a person asks of
	their own list, and it is what a hand-back reads.
	"""

	task = make(pair, "Something to pass on")
	local, remote = pair.both()

	handed = local.update(ref=task.ref, assignee=pair.user.username)

	assert handed.assignee_id == pair.user.id
	assert handed.assigned_by_id == pair.user.id, "the assigner is taken, never accepted"

	# And the other transport reads back what the first one wrote, which is the equivalence
	# that matters here: two clients disagreeing about who holds a task is worse than neither
	# being able to say.
	read_back = remote.task(ref=task.ref)

	assert read_back is not None and read_back.assignee_id == pair.user.id

	given_back = remote.update(ref=task.ref, assignee=None)

	assert given_back.assignee_id is None
	assert given_back.assigned_by_id is None, (
		"an assigner with no assignee names nobody, so clearing one clears both"
	)


def test_both_refuse_to_hand_work_to_somebody_who_is_not_here (pair: Pair) -> None:
	"""`#493`. Refused by name, on both, with the members listed.

	**Workspace-scoped, deliberately unlike the listing filter.** Asking what is assigned to
	Jo is a fair question in a workspace Jo has never joined; *giving* Jo work there is not —
	they could not see it. `tasks.assignee_for` narrows and `selection.user` does not, and the
	two answer different questions with the same grammar.
	"""

	task = make(pair, "Something to pass on")
	local, remote = pair.both()

	for client in (local, remote):
		with pytest.raises(subroutine.errors.ValidationError) as refused:
			client.update(ref=task.ref, assignee="nobody-by-that-name")

		assert "nobody-by-that-name" in str(refused.value)


def test_both_change_a_deadline_and_its_tags_after_the_fact (pair: Pair) -> None:
	"""`#493`, which `#431` was merged into. Set at capture and never changeable.

	A deadline that cannot move is not a deadline, it is a note about what somebody once
	intended; and a mistyped tag could not be taken off. Both were accepted by `PATCH
	/v1/tasks` since M1 and passed by no client.
	"""

	task = make(pair, "Renew the domain #admin")
	local, remote = pair.both()

	moved = local.update(ref=task.ref, due="2026-12-25", tags=["admin", "money"])

	assert moved.due_at is not None and moved.due_at.date().isoformat() == "2026-12-25"
	assert set(moved.tags) == {"admin", "money"}

	# Replaced rather than added to, which is what §8.3 means by a field — and `[]` is how a
	# tag somebody mistyped comes off at all.
	cleared = remote.update(ref=task.ref, tags=[])

	assert cleared.tags == []
	assert cleared.due_at is not None, "clearing tags must not disturb the deadline"


#: Methods a raw call must be refused for, and the same input must be refused *identically*
#: whichever transport is holding it (`#530`). Each was measured against a real instance and a
#: real socket before the fix: in process every one of them reached the router and got a 405,
#: while over HTTP ``BREW`` was a 400 from the server and the rest were refused by h11 at write
#: time and reported as *"could not be reached … Illegal method characters"*.
MALFORMED_METHODS = (
	"BREW",
	"GET\r\nX-Smuggled: 1",
	"GET\nX-Smuggled: 1",
	"",
	"TRACE",
	"CONNECT",
)


def test_both_transports_refuse_the_same_malformed_method (pair: Pair) -> None:
	"""`#530`. One argument got three different answers depending on how you were connected.

	**What this fixture can and cannot show, said plainly.** The remote client here runs over
	httpx's ASGI transport, so h11 is not in the path and the *original* HTTP failure — a
	protocol error at write time, surfacing as `ServiceUnavailable` and blaming the network for
	the caller's own argument — cannot be reproduced here. That half was measured by hand
	against the served instance and is recorded on the item and in `require_a_method`.

	What this does hold is the fix: the method is refused where the argument is *read*, so
	neither transport is ever asked to carry it. Falsifiable either way — before the fix these
	inputs returned a status rather than raising, on both sides.
	"""

	for method in MALFORMED_METHODS:
		answers = []

		for client in pair.both():
			with pytest.raises(subroutine.errors.SubroutineError) as refused:
				client.call_api(method=method, path="/v1/meta")

			answers.append(str(refused.value))

			assert "reach" not in str(refused.value).lower(), (
				f"{method!r} was refused by blaming the network: {refused.value}"
			)

		assert answers[0] == answers[1], (
			f"{method!r} is refused differently by the two transports:\n"
			f"  local:  {answers[0]}\n  remote: {answers[1]}"
		)


def test_both_transports_normalise_an_acceptable_method_the_same_way (pair: Pair) -> None:
	"""Case and surrounding space are the caller's typing, not a different request.

	The half that stops the fix being "refuse anything unfamiliar": `require_a_route` already
	strips, `call_api` already upper-cased, and both transports must go on agreeing about what
	that leaves. `'GET '` used to be a 405 in process and a protocol error over a socket.
	"""

	for method in ("get", "GET ", " get\t", "GET"):
		statuses = [client.call_api(method=method, path="/v1/meta").status for client in pair.both()]

		assert statuses == [200, 200], f"{method!r} answered {statuses}"


def test_every_method_the_application_mounts_can_be_called_raw () -> None:
	"""The allow-list is checked against the routes rather than maintained beside them.

	`call_api` exists so an agent can reach a route nobody wrote a method for (`#485`), so a
	verb missing from `CALLABLE_METHODS` would make a route unreachable through the one thing
	built to reach everything — silently, and only for whoever needed that route.
	"""

	mounted = subroutine.api.routing.mounted(subroutine.api.app.ROUTERS)
	used = {method for _path, methods, _route in mounted for method in methods}

	assert used, "the walk found no routes at all, so it is not measuring the application"

	assert used <= subroutine.clients.base.CALLABLE_METHODS, (
		"a route is mounted with a method a raw call cannot present: "
		f"{sorted(used - subroutine.clients.base.CALLABLE_METHODS)}"
	)


def test_a_raw_call_cannot_be_pointed_at_anything_but_the_api (pair: Pair) -> None:
	"""`#557`. `#484` asked what the escape hatch may *do* and not what it may be pointed at.

	`subroutine_call_api` reached `POST /mcp` — the endpoint that hosts `subroutine_call_api` —
	so one request could nest until the instance stopped answering anybody. Driven on a served
	instance before the fix: depth 5 answered in 5.1s, depth 20 did not answer in 30s, and with
	a depth-30 request in flight `/readyz` timed out twice at 8s. A `task:read` credential was
	enough, because every control this surface has sits below the recursion.

	Refused identically on both transports, because a rule enforced on one of them is the
	divergence this file exists to find.
	"""

	for path in ("/mcp", "/healthz", "/readyz", "/", "/mcp/"):
		refusals = []

		for client in pair.both():
			with pytest.raises(subroutine.errors.SubroutineError) as refused:
				client.call_api(method="GET", path=path)

			refusals.append(str(refused.value))

		assert refusals[0] == refusals[1], f"{path!r} is refused differently: {refusals}"
		assert "/v1" in refusals[0], f"say where a raw call may go: {refusals[0]}"

	# And the API itself is untouched, so this is a boundary rather than a wall.
	assert all(
		client.call_api(method="GET", path="/v1/meta").status == 200 for client in pair.both()
	)


def test_every_route_a_raw_call_is_meant_to_reach_is_under_the_api_prefix () -> None:
	"""The positive rule, checked against the routes rather than maintained beside them.

	A deny-list names what is forbidden and goes stale when a route is added; this names what is
	*allowed* and is compared with what the application mounts. The exclusions are stated rather
	than implied, so a route outside `/v1` is a decision somebody takes.

	`/signin` is the fourth, and it is outside for the same reason `/mcp` is (`#248`): a person
	opens it in a browser, where `/v1/…` is a path nobody types and a link is something you
	read out over a telephone. It is a *browser* surface rather than an API one, which is also
	why no client reaches it — `test_reach` records that beside this.

	`/` and `/app/{name}` are the app itself (`#597`), and the same argument again with the
	volume turned up: the address somebody is *given* is the instance's root, so anything else
	would be a URL a person had to be told. Both are HTML and JavaScript rather than answers,
	and neither reads the database at all.

	**The addresses a person pastes into a message are not routes at all** (`#648`). They were,
	briefly, and `/{workspace}/{project}` claimed `/v1/nothing` — so the API's own 404 became a
	page. They are a 404 fallback now, which is why nothing new appears in this set: an address
	nobody claimed is answered without ever being declared.
	"""

	mounted = subroutine.api.routing.mounted(subroutine.api.app.ROUTERS)
	outside = {
		path
		for path, _methods, _route in mounted
		if not path.startswith(f"{subroutine.clients.base.API_PREFIX}/")
	}

	assert mounted, "the walk found no routes at all, so it is not measuring the application"

	assert outside == {"/healthz", "/readyz", "/mcp", "/signin", "/", "/app/{name}"}, (
		"a route appeared outside the API prefix, so a raw call cannot reach it. That is either "
		"correct and belongs in this list with a reason, or the route is API and is misplaced.\n"
		f"found: {sorted(outside)}"
	)


def test_both_narrow_to_a_status_category_and_reach_finished_work (pair: Pair) -> None:
	"""`#710`. The filter a board and a completed-work view ask with, on both transports.

	**Written because nothing else could see it.** ``test_reach``'s filter guard reads the
	*signature* on :class:`subroutine.clients.base.Client`, so a filter declared there and
	dropped by ``clients/http.py`` reports as reached — falsified by deleting the line that
	sends it, which left the whole suite green. This is what fails in that case.

	The implication is the other half: ``status_category="done"`` reaches finished work without
	``include_completed``, and doing that on one transport only would be the divergence §13.7
	exists to prevent.
	"""

	local, remote = pair.both()

	finished = make(pair, "Finished")
	underway = make(pair, "Underway")
	make(pair, "Not started")

	local.update(ref=underway.ref, status="in_progress")
	local.complete(ref=finished.ref)

	for wanted, expected in (("done", {finished.ref}), ("in_progress", {underway.ref})):
		here = {task.ref for task in local.tasks(status_category=wanted)}

		assert here == {task.ref for task in remote.tasks(status_category=wanted)}
		assert here == expected, f"{wanted!r} did not narrow to the task in it"

	# The `todo` category holds three seeded keys, which is the whole reason for the filter:
	# asking by key means knowing all three and re-learning them when an installation adds one.
	waiting = {task.ref for task in local.tasks(status_category="todo")}

	assert waiting == {task.ref for task in remote.tasks(status_category="todo")}
	assert finished.ref not in waiting


def test_both_refuse_asking_for_finished_work_and_excluding_it (pair: Pair) -> None:
	"""`#710`. A contradiction is named identically whichever transport carried it.

	This is what makes ``include_completed`` three-valued on the wire: sending nothing for
	``False`` would make "no finished work" and "did not say" one request, and the refusal
	could then fire locally and never remotely.
	"""

	for client in pair.both():
		with pytest.raises(subroutine.errors.ValidationError) as raised:
			client.tasks(status_category="done", include_completed=False)

		assert "include_completed" in str(raised.value.errors[0].field)


def test_both_refuse_a_status_category_a_task_cannot_be_in (pair: Pair) -> None:
	"""A document's vocabulary is refused by name rather than matching nothing."""

	for client in pair.both():
		with pytest.raises(subroutine.errors.ValidationError) as raised:
			client.tasks(status_category="superseded")

		assert "cancelled" in str(raised.value.errors[0].hint)


@pytest.mark.parametrize(
	"filters",
	[
		{"created_at.gte": "today"},
		{"created_at.lt": "today"},
		{"created_at.gte": "now-30d", "created_at.lt": "tomorrow"},
		{"updated_at.gte": "start_of_week"},
	],
)
def test_both_narrow_by_a_date_the_same_way (
	pair: Pair, filters: dict[str, str]
) -> None:
	"""§9.6's dotted filters, over both transports — `#815`.

	**The two sides do genuinely different things here**, which is why this is worth a case
	rather than being covered by the listing test above: the local client compiles a predicate
	against its own session, and the HTTP client puts the same words in a query string for the
	far end to compile. One boundary rule, two places it could be applied — and a disagreement
	would be a listing that answers differently depending on where the database is, which is
	the divergence §13.7 exists to prevent.
	"""

	made = [make(pair, f"Task number {index}") for index in range(3)]

	# **One of them backdated, so every case below has a mixed answer.** Without it three of
	# the four filters returned the whole set either way — so a transport that ignored filters
	# altogether was caught by exactly one case, and a parametrisation whose other rows cannot
	# fail is a parametrisation pretending to be four tests.
	pair.session.execute(
		sqlalchemy.update(subroutine.db.models.work.Task)
		.where(subroutine.db.models.work.Task.id == made[0].id)
		.values(
			created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC),
			updated_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC),
		)
	)
	pair.session.flush()

	local, remote = pair.both()

	assert local.tasks(filters=filters) == remote.tasks(filters=filters)
	assert local.documents(filters=_shared(filters)) == remote.documents(
		filters=_shared(filters)
	)


def _shared (filters: dict[str, str]) -> dict[str, str]:
	"""Keep only the fields a document has, since it is not scheduled (§6.14)."""

	return {
		name: value
		for name, value in filters.items()
		if name.partition(".")[0] in subroutine.domain.filtering.DOCUMENT_FILTERS
	}


def test_both_refuse_an_unknown_filter_field_the_same_way (pair: Pair) -> None:
	"""A misspelling is refused by name on either side, rather than matching nothing.

	The local client could have quietly ignored it — it holds the registry and could have
	skipped what it did not recognise — and that would be the failure `api/query.py` exists to
	prevent, reproduced on the transport where nothing was watching for it.
	"""

	for client in pair.both():
		with pytest.raises(subroutine.errors.ValidationError) as refused:
			client.tasks(filters={"creatd_at.gte": "today"})

		assert "creatd_at" in str(refused.value)


def test_both_answer_what_was_worked_on_the_same_way (pair: Pair) -> None:
	"""`touched_at` over both transports — `#815`, and the one filter that leaves the row.

	Worth its own case beside the date filters above because the two sides do more here than
	compile the same comparison twice: the local client builds a correlated `EXISTS` against
	its own session, and the HTTP client sends the words for the far end to build. A comment is
	the discriminating fact — it moves nothing on the item, so a listing that quietly fell back
	to `updated_at` would answer *nothing* on one side and the task on the other.
	"""

	made = make(pair, "Fix the boiler")
	local, remote = pair.both()

	pair.session.execute(
		sqlalchemy.update(subroutine.db.models.work.Task)
		.where(subroutine.db.models.work.Task.id == made.id)
		.values(updated_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))
	)
	pair.session.flush()

	touched = {"touched_at.gte": "today"}

	assert local.tasks(filters=touched) == remote.tasks(filters=touched)
	assert [task.title for task in local.tasks(filters=touched)] == ["Fix the boiler"]

	# The row's own clock still says nothing happened, which is the whole point.
	stale = {"updated_at.gte": "today"}

	assert local.tasks(filters=stale) == remote.tasks(filters=stale) == []


def test_both_resolve_a_username_the_same_way (pair: Pair) -> None:
	"""`touched_by` takes a name, and an unknown one is refused identically on either side.

	The local client could have matched it against nothing and returned an empty list, which
	is the failure mode `#501` named when the same question was asked of `assignee`: a filter
	that silently matches nobody reads as *this person did no work*.
	"""

	make(pair, "Fix the boiler")

	for client in pair.both():
		with pytest.raises(subroutine.errors.NotFound):
			client.tasks(filters={"touched_by.eq": "nobody-at-all"})

	mine = {"touched_by.eq": pair.user.username}

	assert pair.local.tasks(filters=mine) == pair.remote.tasks(filters=mine)
	assert [task.title for task in pair.local.tasks(filters=mine)] == ["Fix the boiler"]


def test_both_tag_a_document_the_same_way (pair: Pair) -> None:
	"""`#819` over both transports, because the two reach the join table differently.

	The local client calls `documents.create` and the HTTP one sends a JSON body for the far
	end to call it — so a field added to one and forgotten in the other is the divergence §13.7
	exists to prevent, and it is invisible to a test that only drives one.
	"""

	local, remote = pair.both()

	here = local.create_document(title="Written here", body=".", tags=["design"])
	there = remote.create_document(title="Written there", body=".", tags=["design"])

	assert here.tags == ["design"]
	assert there.tags == here.tags

	# **One vocabulary**, which is what makes the two documents' tags the same row rather than
	# two rows spelled alike — Simon's decision of 2026-08-12. Counted in the database, since
	# that is where "one row or two" is a fact rather than a rendering.
	assert pair.session.scalar(
		sqlalchemy.select(sqlalchemy.func.count()).select_from(
			subroutine.db.models.vocabulary.Tag
		)
	) == 1


def test_both_replace_a_documents_tags_the_same_way (pair: Pair) -> None:
	"""§8.3 through both, including the two nulls that are easy to get backwards.

	Omitting the field leaves them alone and sending an empty list clears them — and the HTTP
	client has to keep those apart on the wire, where the local one has an `UNSET` sentinel in
	hand. That asymmetry is exactly where this would diverge.
	"""

	for client in pair.both():
		written = client.create_document(
			title=f"Via {client.connection.name}", body=".", tags=["draft", "api"]
		)

		assert written.tags == ["api", "draft"]

		narrowed = client.update_document(ref=written.ref, tags=["api"])

		assert narrowed.tags == ["api"]

		untouched = client.update_document(ref=written.ref, title="Renamed")

		assert untouched.tags == ["api"], "an omitted field cleared them"

		cleared = client.update_document(ref=written.ref, tags=[])

		assert cleared.tags == []


def test_an_items_record_is_readable_after_it_goes_to_the_trash (pair: Pair) -> None:
	"""`#700`. One database, one moment, two clients, opposite answers.

	``_subject`` — the lookup that turns a ref into the id a comment or a link hangs off —
	excluded deleted rows locally, where ``api/tasks._resolve`` has included them since `#140`
	on the argument that *a reference to something in the trash is more useful than a dangling
	one*. Its own docstring asserted the two transports resolve identically, which is this
	project's recorded shape of a claim nothing checks.

	**What made it expensive is where the refusal surfaced.** ``subroutine show`` resolves the
	item, then asks separately for its links and its comments. The item was found; the comments
	lookup refused with *"There is no task #1 here."*, and that is what a reader saw — so the
	command denied the item existed while ``list --trash`` listed it and ``restore`` worked on
	it.

	The divergence was not in what either client *returns*: ``task()`` agrees on both, deleted
	or not. It was in what one of them **asks for**, one layer down.

	**And it was in two sub-resources, not one, which is why this asks for all three.** Fixing
	the comments lookup moved the refusal to the *children* lookup, which resolves the parent
	through its own statement and refused a deleted one where HTTP answered with an empty
	list. The message changed, so it read as progress; it was the next site in the same chain.
	``show`` asks for links, comments and children separately, so any one of them refusing
	looks to a reader exactly like the item not existing.
	"""

	made = pair.local.capture(text="Something to bin")
	ref = made.task.ref

	pair.local.remark(ref=ref, body="said before it went")
	pair.local.discard(ref=ref)
	pair.session.flush()
	pair.session.expire_all()

	for client in pair.both():
		found = client.task(ref=ref)

		assert found is not None, "a ref in the trash still names something"
		assert found.deleted_at is not None

		remarks = client.comments(ref=ref, entity_type="task")

		assert [remark.body for remark in remarks] == ["said before it went"]
		assert client.links(ref=ref, entity_type="task") == []

		# The third sub-resource, and the one that was still refusing after the first fix.
		assert client.tasks(parent=ref, include_completed=True) == []


def test_a_person_can_ask_for_their_own_work_by_name_or_by_me (pair: Pair) -> None:
	"""`#518`, Simon's: *"can I filter to only view tasks assigned to me?"*

	**`me` is the account, where `?actor=me` on the change feed is the credential** (`#158`).
	Two spellings of one word for two different things, which `#335` measured — an agent with a
	shell is two principals — so the field is what settles which is meant: *who did this* is a
	question about a credential, *who holds this* is a question about an account.

	Driven through both clients, because a sentinel resolved in one of them would be the
	divergence `test_reach` exists to prevent and the shape `#700` cost two commits.
	"""

	made = pair.local.capture(text="Something for me")

	pair.local.update(ref=made.task.ref, assignee=pair.user.username)
	pair.session.flush()
	pair.session.expire_all()

	for client in pair.both():
		named = client.tasks(assignee=pair.user.username)
		mine = client.tasks(assignee="me")

		assert [task.ref for task in mine] == [made.task.ref]
		assert [task.ref for task in named] == [task.ref for task in mine]


def test_me_is_not_a_way_to_ask_about_somebody_whose_name_you_do_not_know (
	pair: Pair,
) -> None:
	"""The sentinel is opt-in per call site, and this is why.

	:func:`subroutine.domain.selection.user` also resolves the account a **sign-in link** is
	minted for and the one signed out of every browser. A sentinel accepted everywhere would
	have widened the grammar of two credential routes as a side effect of adding a listing
	filter — which is `#829`'s shape, where a route nobody thought about was the one that
	mattered.

	So those callers pass no ``caller`` and ``me`` stays an ordinary username there, refused by
	name like any other that does not exist.

	**Signing out rather than minting a link, and that is a correction.** The first version of
	this asked for a login link and was answered *"this instance has no public_url"* — a
	refusal from a check two steps earlier, so it would have passed whatever ``me`` resolved
	to. A test that refuses for the wrong reason is one that cannot fail.
	"""

	with pytest.raises(subroutine.errors.SubroutineError) as refused:
		pair.local.sign_out_everywhere(username="me")

	assert "'me'" in str(refused.value), (
		"the sentinel leaked into a credential route, where it names an account nobody typed"
	)


def test_the_size_reported_is_bytes_on_the_wire_rather_than_characters (pair: Pair) -> None:
	"""`#595`. The unit is the whole reason the number is trustworthy.

	An em dash is one character and three bytes, and this project writes in them — so a
	character count understates exactly the prose worth warning about, by up to a third, and
	does it silently. A reader deciding whether to spend a context window on a document is
	spending bytes.

	Written because the mutation that swapped ``len(text.encode("utf-8"))`` for ``len(text)``
	passed every other test here: the mark still appeared, the field was still populated, and
	nothing anywhere compared the figure against the thing it measures.
	"""

	prose = "— " * 500
	made = pair.local.capture(text="Something with punctuation in it")

	pair.local.update(ref=made.task.ref, description=prose)
	pair.session.flush()
	pair.session.expire_all()

	for client in pair.both():
		found = client.task(ref=made.task.ref)

		assert found is not None
		assert found.size_bytes == len(prose.encode("utf-8"))
		assert found.size_bytes > len(prose), (
			"counted in characters, which understates every document this project writes"
		)


@pytest.fixture
def native (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Ask for the indexed backend before anything reads settings.

	**Ordered ahead of ``pair`` in the signature deliberately.** Both clients resolve settings
	when they are built, so setting this inside the test body is too late for the HTTP one —
	which is how the first version of this test had a *local* client ranking correctly and a
	*remote* one not, and looked exactly like the divergence it was written to catch.
	"""

	monkeypatch.setenv("SUBROUTINE_SEARCH_BACKEND", "native")


def test_both_rank_a_search_the_same_way (native: None, pair: Pair) -> None:
	"""`#823`'s ranking on both transports, because the local client orders its own query.

	`ordering.py` exists because a sort applied on one side and not the other is the same
	divergence S3-07 removed for the task *shape*. A ranking is the newest sort field and the
	first that is not a column, so it is the likeliest to land on one transport only.

	Skipped where nothing can rank: the native backend is PostgreSQL-only by decision (`#871`),
	and on SQLite both sides correctly fall back to the same unranked order.
	"""

	if pair.session.get_bind().dialect.name != "postgresql":
		pytest.skip("relevance needs a backend that can rank")

	subject = make(pair, "Entirely unlike anything")

	for number in range(4):
		make(pair, f"Mentions it {number}")
		mentioned = pair.local.tasks(limit=1)[0]
		row = pair.session.get(subroutine.db.models.work.Task, mentioned.id)

		assert row is not None

		subroutine.domain.tasks.update(
			pair.session, row, description=f"Follows on from #{subject.ref}."
		)

	pair.session.flush()

	local, remote = pair.both()
	asked = str(subject.ref)

	assert next(task.ref for task in local.tasks(q=asked, limit=50)) == subject.ref
	assert [task.ref for task in local.tasks(q=asked, limit=50)] == [
		task.ref for task in remote.tasks(q=asked, limit=50)
	]


def test_both_answer_a_search_with_no_words_in_it (native: None, pair: Pair) -> None:
	"""`SR#880`. The local client reached the same unguarded ranking, so MCP did too.

	**This is the surface the crash actually mattered on.** `subroutine search` and
	`subroutine_search` both strip before they ask, so the two commands somebody thought about
	were safe — while `GET /v1/tasks`, `subroutine list -q`, `subroutine_list(q=…)` and the
	browser's search box all sent the raw string and got a 500.

	Both transports, because the fix is applied in four places and a client that resolved its
	own backend differently is exactly what `SR#883` turned out to be.
	"""

	for title in ("Read the backlog", "Write it down"):
		make(pair, title)

	local, remote = pair.both()

	assert [task.ref for task in local.tasks(q="  ")] == [
		task.ref for task in remote.tasks(q="  ")
	]
	assert len(local.tasks(q="  ")) == 2, "a query with no words in it narrowed something"
	assert [task.ref for task in local.tasks(q="  ")] == [task.ref for task in local.tasks()], (
		"a listing with nothing to search for should be the listing with no search"
	)

	assert len(local.documents(q=" ")) == len(remote.documents(q=" "))


def test_both_refuse_a_ranking_on_a_listing_that_is_not_a_search (
	native: None, pair: Pair
) -> None:
	"""**`SR#884`. A name `/v1/meta` publishes must not come back as an unknown field.**

	`relevance` is published wherever a backend can rank, because that is what the instance can
	do — and it enters a *request's* vocabulary only when there is a search to rank. So asking
	for it without one was answered *"'relevance' is not a field this listing can sort by"*,
	about a field the same instance advertises, and which `README.md` tells a client to rely on
	in as many words:

	> each listing's `sortable` names `relevance` **exactly when it can be ordered by one**. Do
	> not infer it from anything else.

	A client doing exactly that met a 422 about the wrong thing.

	**Both transports, and both messages**, because a refusal is part of the contract: an agent
	learns the rule from what it is told, and learning two different rules depending on how it
	connected is what this suite exists to prevent.
	"""

	make(pair, "Something to find")

	for client in pair.both():
		with pytest.raises(subroutine.errors.ValidationError) as raised:
			client.tasks(order="-relevance")

		assert raised.value.errors[0].field == "order"
		assert "search" in raised.value.errors[0].message, (
			f"the refusal must name what is missing, not the field — {raised.value.errors[0]}"
		)

		# And it is still answered when there *is* one, which is the half that says the
		# refusal is about the request rather than about the name.
		#
		# **Only where something can rank.** On SQLite `native` falls back, so `relevance` is
		# in no vocabulary at all and this refusal is the right answer there too — asserting
		# the opposite would be asserting a backend rather than the rule.
		if pair.session.get_bind().dialect.name == "postgresql":
			assert client.tasks(q="something", order="-relevance") is not None

	for client in pair.both():
		with pytest.raises(subroutine.errors.ValidationError):
			client.documents(order="relevance")


def test_both_re_parent_the_same_way (pair: Pair) -> None:
	"""`#44`. Two implementations of one operation, which is what this file is for.

	The local client resolves the parent through its own helper and the HTTP one hands the ref
	to a route — so "an unknown parent is refused" is a claim about two pieces of code, and the
	failure it would hide is the worst available: a parent that could not be found resolving to
	``None``, which is *a real value here* and means the top level. Silently promoting an item
	instead of refusing is the shape this whole file exists to catch.
	"""

	local, remote = pair.both()

	parent = make(pair, "The parent")
	elsewhere = make(pair, "Somewhere else")
	child = make(pair, "The child")

	def parent_of (item: subroutine.views.Task | subroutine.views.Document | None) -> typing.Any:
		"""Read the parent off whichever kind came back, refusing anything else."""

		assert isinstance(item, subroutine.views.Task), f"expected a task, got {item!r}"

		return item.parent_task_id

	# Moved by one transport, read back through the other.
	assert parent_of(local.move(ref=child.ref, parent=parent.ref)) == parent.id
	assert parent_of(remote.task(ref=child.ref)) == parent.id

	assert parent_of(remote.move(ref=child.ref, parent=elsewhere.ref)) == elsewhere.id
	assert parent_of(local.task(ref=child.ref)) == elsewhere.id

	# **Null is a value, and both have to treat it as one.**
	assert parent_of(remote.move(ref=child.ref, parent=None)) is None
	assert parent_of(local.task(ref=child.ref)) is None

	# And a parent that is not there is refused rather than read as "the top level".
	for client in (local, remote):
		with pytest.raises(subroutine.errors.NotFound):
			client.move(ref=child.ref, parent=99999)

	assert parent_of(local.task(ref=child.ref)) is None, "and nothing moved"
