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
