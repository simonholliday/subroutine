"""Saving a view and sharing one — `SR#1402`, Simon's decision of 2026-09-20.

**A saved view is a query and an arrangement**: one search line for what is narrowed, and three
named fields for how it is drawn. The tests here are written around the two things that can go
quietly wrong rather than around the shape of the endpoints.

**The first is a stored 422.** An order or an axis a listing cannot answer is refused when the
view is *written*, because by the time somebody opens a saved board the person who saved it is
gone and the page has no way to say what is wrong with it. So the refusals are asserted at the
write, and one test asserts the saved view really does apply afterwards.

**The second is a selection leaking into the arrangement half.** `SR#649` and `SR#718` took
that shape out of the browser's address, where a view *name* silently appended a filter and
made the filter unreachable on its own. Here it would arrive as an arrangement field that
narrows, so the arrangement is pinned to exactly three names.
"""

import typing

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.api.saved
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.saved
import subroutine.domain.authentication
import subroutine.domain.saved
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.permissions
import subroutine.views
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _somebody_else (world: test_api_tasks.World, who: str = "keanu") -> str:
	"""Make a second real person in this workspace and return a token that is theirs.

	**A person, not an agent.** `SR#1432` is the recorded cost of using an agent as the
	stand-in for *somebody else*: an agent answers to its operator, so half the questions
	about *whose is this* have a different answer for one and the tests stopped meaning what
	their names said.

	**A member, not merely an account**, which the workspace refuses by name otherwise.
	"""

	person = subroutine.domain.users.create(world.session, username=who)
	subroutine.domain.workspaces.add_member(
		world.session, world.workspace, person, role_key="member"
	)
	world.session.flush()

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=person, title=f"{who}'s token"
	)
	world.session.flush()

	return str(issued.value.get_secret_value())


def _as (world: test_api_tasks.World, secret: str, method: str, path: str, **kwargs: typing.Any) -> typing.Any:
	"""Make a request as somebody other than the world's own account."""

	headers = {"authorization": f"Bearer {secret}", **kwargs.pop("headers", {})}

	return api_support.call(
		world.application, method, path, headers=headers, **kwargs
	)


def _saved (world: test_api_tasks.World, **body: typing.Any) -> dict[str, typing.Any]:
	"""Save a view and return it, failing loudly if it was refused."""

	body.setdefault("title", "Team queue")
	body.setdefault("arrangement", "board")
	made = world.call("POST", "/v1/views", json=body)

	assert made.status_code == 201, made.text

	payload: dict[str, typing.Any] = made.json()

	return payload


def test_a_saved_view_keeps_a_query_and_an_arrangement_under_a_name (
	world: test_api_tasks.World,
) -> None:
	"""The whole of `SR#1402`, round-tripped: both halves go in and both come back.

	**Two halves and nothing else.** The query is one search line in the grammar `SR#1806`
	put on `q`, which narrows and searches at once; the arrangement is which arrangement, the
	order and the axis. A view that came back missing either half would be the complaint the
	item was filed about — *a saved board comes back as a list*.
	"""

	view = _saved(
		world,
		title="Team queue",
		arrangement="board",
		q="type:bug urgency>=4 deploy",
		order="-created_at",
		group_by="assignee",
	)

	assert view["q"] == "type:bug urgency>=4 deploy"
	assert view["arrangement"] == "board"
	assert view["order"] == "-created_at"
	assert view["group_by"] == "assignee"

	read = world.call("GET", "/v1/views/team-queue")

	assert read.status_code == 200, read.text
	assert read.json() == view, "reading a view back gave something other than what was saved"


def test_a_views_address_is_derived_from_the_name_somebody_typed (
	world: test_api_tasks.World,
) -> None:
	"""One name in, one address out — the two-step `init` already uses for a workspace.

	**Derived rather than typed**, so a caller sends one fact and not two. A name and an
	address that can disagree are two copies of one thing, which is this codebase's signature
	defect; and shaping a *derived* value is the service doing its job rather than the silent
	correction `projects.normalize_key` refuses to make to a key somebody typed on purpose.

	**The title is kept verbatim**, because the key is lossy by construction and a list showing
	`team-queue` where its author wrote `Team queue` is a list of slugs.
	"""

	view = _saved(world, title="  Team Queue — Bugs!  ")

	assert view["key"] == "team-queue-bugs"
	assert view["title"] == "Team Queue — Bugs!"
	assert world.call("GET", "/v1/views/team-queue-bugs").status_code == 200


def test_a_view_is_mine_until_i_share_it (world: test_api_tasks.World) -> None:
	"""`SR#1402`'s second decision, both directions.

	**Mine by default and shared on purpose.** Somebody else's private view is *not found*
	rather than *forbidden*, because a 403 would confirm the name is in use to anybody who
	guessed it — the distinction `authorization.ProjectNotVisible` already draws one entity
	over.
	"""

	keanu = _somebody_else(world)

	_saved(world, title="My scratch", arrangement="list")

	assert _as(world, keanu, "GET", "/v1/views/my-scratch").status_code == 404
	assert [row["key"] for row in _as(world, keanu, "GET", "/v1/views").json()["items"]] == []

	assert world.call(
		"PATCH", "/v1/views/my-scratch", json={"shared": True}
	).status_code == 200

	assert _as(world, keanu, "GET", "/v1/views/my-scratch").status_code == 200
	assert [row["key"] for row in _as(world, keanu, "GET", "/v1/views").json()["items"]] == [
		"my-scratch"
	]


def test_a_views_name_is_taken_whether_or_not_it_is_shared (
	world: test_api_tasks.World,
) -> None:
	"""The cost of one name meaning one view, asserted rather than left implied — `SR#1402`.

	**This is the price of a shared address being honest.** Resolving *mine first, then the
	workspace's* would need no uniqueness at all and reads better — and it is the defect
	`SR#745` refused ``me`` for: a link somebody sends would draw the recipient's own queue
	rather than what the sender was looking at.

	So a private view still takes the word, and the refusal names the address without saying
	whose it is. That leaks exactly what a username or a project key leaks — that a name is
	taken — which is the smallest leak a unique namespace has.
	"""

	keanu = _somebody_else(world)

	_saved(world, title="Queue", arrangement="list")

	refused = _as(
		world, keanu, "POST", "/v1/views", json={"title": "Queue", "arrangement": "board"}
	)

	assert refused.status_code == 409, refused.text
	assert "queue" in refused.text

	# **And it does not say whose**, which is the half that makes the leak the small one.
	assert world.user.username not in refused.text


def test_a_view_cannot_be_named_after_an_arrangement (world: test_api_tasks.World) -> None:
	"""`board` is how a view is drawn, so it cannot also be the name of one.

	Without this, one address would mean two things and which won would depend on lookup order
	rather than on anything anybody decided. The refusal lists the three, because a reader who
	has just been told their name is reserved wants to know what else is.
	"""

	refused = world.call("POST", "/v1/views", json={"title": "Board", "arrangement": "board"})

	assert refused.status_code == 422, refused.text
	assert "board" in refused.text
	assert "agenda" in refused.text, "the refusal did not say what else is reserved"


def test_an_order_a_listing_cannot_answer_is_refused_when_the_view_is_saved (
	world: test_api_tasks.World,
) -> None:
	"""Refused at the write, because at the read there is nobody left to tell.

	A saved view holding an order the listing does not have is a 422 stored for later, and the
	person who opens it did not write it and cannot fix it. Checked against
	``ordering.TASK_FIELDS``, which is the same map the listing itself sorts by — so this
	cannot drift into a fourth opinion about what a listing can do.
	"""

	refused = world.call(
		"POST",
		"/v1/views",
		json={"title": "Wrong", "arrangement": "list", "order": "-whenever"},
	)

	assert refused.status_code == 422, refused.text
	assert "whenever" in refused.text
	assert "created_at" in refused.text, "the refusal did not say what can be ordered by"


#: Eight fields every listing sorts by, and 85 characters - longer than the column holds.
_LONG_AND_VALID = (
	"-priority_score,created_at,-updated_at,due_at,-importance,urgency,-completed_at,title"
)


@pytest.mark.parametrize(
	("order", "said", "status"),
	(
		("--priority_score", "not a field", 422),
		("created_at,created_at", "twice", 422),
		# Too long for the column, which is refused as every such value is - 413, by name.
		(_LONG_AND_VALID, "sort order", 413),
	),
	ids=("two dashes", "one field twice", "longer than the column"),
)
def test_an_order_every_run_would_refuse_is_refused_when_it_is_saved (
	world: test_api_tasks.World, order: str, said: str, status: int
) -> None:
	"""`SR#3141`, the cold review of 2026-09-21's M-3: a looser copy of the listing's parser.

	The check stripped every dash and never looked for a repeat, so the first two were saved
	and every run of them refused - the 422 stored for somebody who did not write it, which the
	test above is about. The third is valid and too long for the column: SQLite stored it and
	PostgreSQL answered a 500. **Saving and editing are both refused**, since each writes it.
	"""

	saved = world.call(
		"POST", "/v1/views", json={"title": "Sorted", "arrangement": "list", "order": order}
	)

	assert saved.status_code == status, saved.text
	assert said in saved.text, saved.text

	made = world.call("POST", "/v1/views", json={"title": "Plain", "arrangement": "list"})

	assert made.status_code == 201, made.text

	edited = world.call("PATCH", "/v1/views/plain", json={"order": order})

	assert edited.status_code == status, edited.text


def test_every_field_of_a_saved_order_is_checked (
	world: test_api_tasks.World,
) -> None:
	"""L-10 of the same review: no test saved an order of more than one field.

	Checking only the first survived every test there was, so the second field of *most
	important, then soonest due* could have been anything at all.
	"""

	saved = world.call(
		"POST",
		"/v1/views",
		json={"title": "Ranked", "arrangement": "list", "order": "-importance,due_at"},
	)

	assert saved.status_code == 201, saved.text
	assert saved.json()["order"] == "-importance,due_at"

	refused = world.call(
		"POST",
		"/v1/views",
		json={"title": "Half right", "arrangement": "list", "order": "-importance,whenever"},
	)

	assert refused.status_code == 422, refused.text
	assert "whenever" in refused.text


def test_an_axis_a_board_cannot_group_by_is_refused_when_the_view_is_saved (
	world: test_api_tasks.World,
) -> None:
	"""The same rule on the other arrangement field, through the grouping registry itself.

	Through ``grouping.refuse_unknown_axis`` rather than a list of its own, so an axis added to
	the registry is savable the day it is added — which is what stopped this becoming a fourth
	place that has to agree about what a board can do.
	"""

	refused = world.call(
		"POST",
		"/v1/views",
		json={"title": "Wrong", "arrangement": "board", "group_by": "due_at"},
	)

	assert refused.status_code == 422, refused.text
	assert "due_at" in refused.text
	assert "status_category" in refused.text, "the refusal did not name a real axis"

	# **And an axis that really is one is accepted**, which is the half that says the refusal
	# is about the registry rather than about refusing everything.
	assert _saved(world, title="By person", arrangement="board", group_by="assignee")[
		"group_by"
	] == "assignee"


def test_only_the_person_who_saved_a_shared_view_may_change_it (
	world: test_api_tasks.World,
) -> None:
	"""Sharing publishes a view; it does not hand it over.

	The comment rule (§5.10) asked about a different noun: *an administrator rewriting
	somebody's words under their name is not a permission anybody should hold*. A shared view
	is one person's statement of how the team's queue is read, and a second person quietly
	changing what everybody's saved link draws is that defect with a larger audience.

	**Read, yes; write, no** — which is why the refusal is a 403 here where an unshared view
	is a 404. By this point the caller can already see it, so there is nothing left to conceal.
	"""

	keanu = _somebody_else(world)

	_saved(world, title="Team queue", arrangement="board", shared=True)

	assert _as(world, keanu, "GET", "/v1/views/team-queue").status_code == 200

	refused = _as(
		world, keanu, "PATCH", "/v1/views/team-queue", json={"title": "Mine now"}
	)

	assert refused.status_code == 403, refused.text
	assert _as(world, keanu, "DELETE", "/v1/views/team-queue").status_code == 403
	assert world.call("GET", "/v1/views/team-queue").json()["title"] == "Team queue"


def test_clearing_a_views_grouping_is_not_the_same_as_not_mentioning_it (
	world: test_api_tasks.World,
) -> None:
	"""``null`` is a value on three fields here, so absence has to mean something else.

	Without this, *ungroup this board* is unaskable: every write would either clear the fields
	it did not mention or be unable to clear the ones it did. `SR#1396` met the same shape one
	surface over — ``scopes: []`` means *no narrowing* where the field's absence means *say
	nothing about it*.
	"""

	_saved(world, title="Team queue", arrangement="board", group_by="assignee", q="type:bug")

	# Not mentioned: left alone.
	kept = world.call("PATCH", "/v1/views/team-queue", json={"title": "Team queue"})

	assert kept.status_code == 200, kept.text
	assert kept.json()["group_by"] == "assignee", "an unmentioned field was cleared"
	assert kept.json()["q"] == "type:bug"

	# Sent as null: cleared.
	cleared = world.call("PATCH", "/v1/views/team-queue", json={"group_by": None})

	assert cleared.status_code == 200, cleared.text
	assert cleared.json()["group_by"] is None, "sending null did not clear the field"
	assert cleared.json()["q"] == "type:bug", "clearing one field cleared another"


def _in_process (world: test_api_tasks.World) -> subroutine.clients.local.Client:
	"""Return a local client on this world's database, holding this world's credential.

	Explicitly credentialled rather than relying on §12.1a's *exactly one account*, because
	these tests make a second person and that rule then refuses every call with *this database
	has more than one account* — `SR#587`'s recorded trap, arriving in a test.
	"""

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=world.user, title="The operator, explicitly"
	)
	world.session.flush()

	return subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(world.session),
		token=issued.value.get_secret_value(),
	)


def test_a_saved_view_is_reachable_through_the_local_client_too (
	world: test_api_tasks.World,
) -> None:
	"""Both clients, because one of them being able to ask is the divergence S3-07 removed.

	`SR#291`, `SR#294` and `SR#300` are three endpoints that shipped since M1 which no client
	could call, all found in one evening and none by the suite. ``test_reach`` measures that a
	method *exists*; this measures that it answers.
	"""

	local_client = _in_process(world)
	saved = local_client.save_view(
		title="Local queue", arrangement="list", q="type:bug", order="-created_at"
	)

	assert saved.key == "local-queue"
	assert saved.arrangement == "list"
	assert saved.order == "-created_at"
	assert local_client.saved_view(key="local-queue").title == "Local queue"
	assert [row.key for row in local_client.saved_views().items] == ["local-queue"]

	local_client.forget_saved_view(key="local-queue")

	assert [row.key for row in local_client.saved_views().items] == []


def test_a_forgotten_view_gives_its_name_back (world: test_api_tasks.World) -> None:
	"""Deleted for good, unlike a task — and the unique constraint is why it matters.

	``SoftDeleteMixin`` exists so work can be restored and so a ref is never reused. A view is
	neither: it records nothing that happened, and one left in the table would go on holding
	its name against the next person who wants it, which is the one thing this table's unique
	constraint is for.
	"""

	_saved(world, title="Queue", arrangement="list")

	assert world.call("DELETE", "/v1/views/queue").status_code == 204
	assert world.call("GET", "/v1/views/queue").status_code == 404

	again = world.call("POST", "/v1/views", json={"title": "Queue", "arrangement": "board"})

	assert again.status_code == 201, "the name did not come back: " + again.text


def test_the_arrangement_half_carries_exactly_three_fields (
	world: test_api_tasks.World,
) -> None:
	"""The bound, asserted where somebody widening this will meet it — `SR#649`, `SR#718`.

	**A selection may not move into the arrangement half.** The browser's address once let a
	view *name* silently append a filter, which made that filter unreachable on its own; the
	same mistake here would be an arrangement field that narrows. So a request naming a
	selection is refused rather than quietly stored, and the reason is that a view's whole
	narrowing lives in ``q`` where anybody can read it.

	`SR#3093` is the one honest gap that leaves: ``status_category`` is a registry property
	with no kind, so it cannot be written as a term and a board of *what the team has in
	progress* is not yet savable.
	"""

	refused = world.call(
		"POST",
		"/v1/views",
		json={"title": "Sneaky", "arrangement": "board", "status_category": "in_progress"},
	)

	assert refused.status_code == 422, refused.text

	saved = subroutine.views.SavedView.model_fields

	assert {"arrangement", "order", "group_by"} <= set(saved)
	assert "status_category" not in saved
	assert "include_completed" not in saved


def test_every_field_a_change_accepts_reaches_the_row (world: test_api_tasks.World) -> None:
	"""`SR#919`'s question asked of this entity: does an accepted field survive the journey?

	**Behavioural rather than structural, and the pair is what makes it worth anything.** Set a
	value and find it there and you may have found what the fixture already held; set a
	*second*, different value and read again, and a field that is accepted and quietly dropped
	cannot pass. Three fields shipped accepted-and-discarded in one arc before `SR#919` existed.

	**Written here rather than driven by `tests/test_api_delivery.py`**, which derives its cases
	from the same `SURFACES` tuple this entity now appears in. That file's generic create cannot
	supply the two fields `POST /v1/views` requires, which is the same obstacle its
	`NOT_DELIVERED` register records for a project's key and a workspace's slug. The population
	here is read off `UpdateView` for the same reason it is derived there: a field added
	tomorrow is a case tomorrow rather than one somebody has to remember.
	"""

	_saved(world, title="Team queue", arrangement="board", q="one", order="title", group_by="assignee")

	# Two different values per field, so *accepted and dropped* cannot look like success.
	rounds: tuple[tuple[str, typing.Any, typing.Any], ...] = (
		("title", "First name", "Second name"),
		("arrangement", "list", "agenda"),
		("q", "type:bug", "type:chore"),
		("order", "-created_at", "title"),
		("group_by", "status_category", "assignee"),
		("shared", True, False),
	)
	declared = set(subroutine.api.saved.UpdateView.model_fields) - {"expected_version"}

	assert declared == {name for name, _first, _second in rounds}, (
		"UpdateView has grown or lost a field and this test was not told: "
		f"{sorted(declared)}"
	)

	at = "team-queue"

	for name, first, second in rounds:
		seen = []

		for value in (first, second):
			answer = world.call("PATCH", f"/v1/views/{at}", json={name: value})

			assert answer.status_code == 200, f"{name}={value!r}: {answer.text}"

			body = answer.json()
			seen.append(body[name])

			# Renaming moves the address, so follow it rather than asking for the old one.
			at = body["key"]

		assert seen == [first, second], (
			f"{name} was accepted and did not reach the row: sent {[first, second]}, "
			f"read back {seen}"
		)


def test_a_credential_that_cannot_read_work_cannot_read_the_saved_questions_about_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1402`: reading a view is `task:read`, and this is what stops that being a sentence.

	**Resolving a workspace says which ones a caller can reach, never what they may do there**,
	so a read path that stopped at the resolver would publish the rule and check nothing - the
	shape `SR#247`, `SR#251` and `SR#303` are all instances of, and the one this codebase finds
	most often. It was that shape here for an afternoon.

	**A saved view is a saved question about work**, and its `q` can say *what is keanu
	holding*, so a credential narrowed away from the work has no business reading the question.
	"""

	world = test_api_tasks._world(session)

	_saved(world, title="Team queue", arrangement="board", q="assignee:keanu", shared=True)

	# The same person, through a credential narrowed to writing tasks and nothing else.
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="Write only", scopes=[subroutine.permissions.TASK_WRITE]
	)
	session.flush()
	narrowed = str(issued.value.get_secret_value())

	assert _as(world, narrowed, "GET", "/v1/views").status_code == 403
	assert _as(world, narrowed, "GET", "/v1/views/team-queue").status_code == 403

	# **And the unnarrowed credential still reads it**, which is the half that says the refusal
	# is about the scope rather than about the route being broken.
	assert world.call("GET", "/v1/views/team-queue").status_code == 200
