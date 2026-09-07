"""Asking a listing about a date over HTTP — item `#815`, decision `#817`.

`tests/test_filtering.py` drives the compiler; this drives the **wire**, which is the half that
has failed six times in this project without anything noticing. Every one of those had the same
shape: the rule was right, the display was right, and nothing joined them. A pure function is
not enough on its own — so what is checked here is that a request narrows a real result set,
against a real instance, over the real route.

**Simon's five questions are the cases**, verbatim from `#815`, because a feature is finished
when the thing it was asked for can be done rather than when its parts exist.
"""

import datetime
import typing
import uuid

import fastapi
import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.api.app
import subroutine.api.filters
import subroutine.api.meta
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.system
import subroutine.db.models.work
import subroutine.db.seed
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.filtering
import subroutine.domain.instances
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

#: Far enough from "now" that nothing here depends on the hour the suite runs at.
#:
#: **The 3rd is midday rather than morning, and that is the whole of the timezone case.** Fourteen
#: hours east, midday UTC on the 3rd is already the 4th — so it is the one row that lands on
#: different sides of *the start of the 4th* depending on which zone the boundary was computed
#: in. Every other hour would answer identically either way, which is how the first version of
#: that test passed against a chain with the workspace removed.
DAYS = {
	"the 1st": datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC),
	"the 3rd": datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC),
	"the 5th": datetime.datetime(2026, 8, 5, 9, 0, tzinfo=datetime.UTC),
}

#: The instance's own zone, pinned so the fixture does not inherit the machine's. `#532` is what
#: happens otherwise: a boundary computed wrongly and a boundary computed in the zone the test
#: runs in are indistinguishable.
INSTANCE_ZONE = "UTC"

#: The workspace's, for the one case that asks whether §6.5's chain reaches the filter at all.
#: UTC+14, chosen because no offset is further from the instance's — a chain that ignored the
#: workspace would have to be wrong by more than half a day to be missed.
FAR_EAST = "Pacific/Kiritimati"


class World(typing.NamedTuple):
	"""An installation with three tasks, created on three known days."""

	application: fastapi.FastAPI
	session: sqlalchemy.orm.Session
	user: subroutine.db.models.identity.User
	workspace: subroutine.db.models.identity.Workspace
	secret: str

	def call (self, method: str, path: str, **kwargs: typing.Any) -> typing.Any:
		"""Make an authenticated request."""

		return api_support.call(
			self.application,
			method,
			path,
			headers={"authorization": f"Bearer {self.secret}"},
			**kwargs,
		)

	def titles (self, path: str) -> list[str]:
		"""Return the titles a listing answers with, so a case reads as the question it asks."""

		answer = self.call("GET", path)

		assert answer.status_code == 200, answer.text

		return sorted(item["title"] for item in answer.json()["items"])


def _instance (
	session: sqlalchemy.orm.Session,
) -> subroutine.db.models.system.Instance:
	"""Return the instance row, which every test here has bootstrapped."""

	found = subroutine.domain.instances.get(session)

	assert found is not None, "the fixture did not bootstrap an instance"

	return found


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> World:
	"""An installation whose three tasks were created on the 1st, the 3rd and the 5th."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Test token"
	)
	_instance(session).timezone = INSTANCE_ZONE

	built = World(
		application=api_support.build_app(api_support.factory_for(session)),
		session=session,
		user=setup.user,
		workspace=setup.workspace,
		secret=issued.value.get_secret_value(),
	)

	for title, when in DAYS.items():
		created = built.call("POST", "/v1/tasks", json={"title": title})

		assert created.status_code == 201, created.text

		# **Backdated after the fact, because the column is written by the database.** The row
		# is what a filter reads, so setting it here is setting exactly what a task created on
		# that day would carry.
		identity = uuid.UUID(created.json()["id"])

		session.execute(
			sqlalchemy.update(subroutine.db.models.work.Task)
			.where(subroutine.db.models.work.Task.id == identity)
			.values(created_at=when, updated_at=when)
		)

		# **And its events with it**, which the first version forgot. `touched_at` reads the
		# feed rather than the row, so three tasks backdated to three days all carried a
		# *created* event stamped now — and every activity question answered "all of them",
		# which looked like a filter that was not applied at all.
		session.execute(
			sqlalchemy.update(subroutine.db.models.activity.Event)
			.where(subroutine.db.models.activity.Event.entity_id == identity)
			.values(created_at=when)
		)

	session.flush()

	return built


def test_a_listing_answers_what_was_created_before_a_day (world: World) -> None:
	"""Simon's fifth question, which is the one the boundary rule can answer wrongly.

	`created_at.lt=2026-08-03` must exclude the whole of the 3rd, and `lte` must take all of
	it. Resolving a day to its midnight — the obvious implementation — gets the first of those
	right for nothing and the second wrong, returning a confidently short list; the pair is
	what tells them apart.
	"""

	assert world.titles("/v1/tasks?created_at.lt=2026-08-03") == ["the 1st"]
	assert world.titles("/v1/tasks?created_at.lte=2026-08-03") == ["the 1st", "the 3rd"]


def test_a_listing_answers_what_was_created_since_a_day (world: World) -> None:
	"""Simon's fourth question in its field form: *since the 2nd August*."""

	assert world.titles("/v1/tasks?created_at.gte=2026-08-03") == ["the 3rd", "the 5th"]
	assert world.titles("/v1/tasks?created_at.gt=2026-08-03") == ["the 5th"]


def test_two_bounds_are_both_applied (world: World) -> None:
	"""A window, which is two parameters of the same name-shape in one query string.

	Worth its own case because a naive reader keyed on the field name would keep one of them,
	and a listing that silently dropped a bound would answer with a *longer* list — which reads
	as correct far more easily than a short one does.
	"""

	assert world.titles(
		"/v1/tasks?created_at.gte=2026-08-02&created_at.lt=2026-08-05"
	) == ["the 3rd"]


def test_a_date_filter_combines_with_every_other_filter (world: World) -> None:
	"""**Simon's requirement, verbatim**: "either on date alone, or in combination".

	This is why it is a filter on the listing rather than a second endpoint. `q` is the hardest
	neighbour to sit beside, because it is the one selection whose values cannot be enumerated
	(`#775`).
	"""

	assert world.titles("/v1/tasks?created_at.gte=2026-08-02&q=3rd") == ["the 3rd"]
	assert world.titles("/v1/tasks?created_at.lt=2026-08-02&q=3rd") == []


def test_a_relative_expression_reaches_the_route (world: World) -> None:
	"""*Yesterday* rather than a literal, which is what an agent will actually send.

	`?due_before=start_of_week+3d` was a 422 saying "invalid character in year" while `/v1/meta`
	published that grammar with examples — the gap `#815` was filed to close. Asserted as "no
	older than a year" rather than by row, because the fixture's days are fixed and *now* is not.
	"""

	answer = world.call("GET", "/v1/tasks?created_at.gte=now-1y")

	assert answer.status_code == 200, answer.text
	assert len(answer.json()["items"]) == 3


def test_the_day_is_read_in_the_callers_timezone (world: World) -> None:
	"""§6.5's whole chain reaches this, which is the step a pure function cannot check.

	The task titled *the 3rd* was created at **midday UTC**. In the instance's zone the 4th
	begins at midnight UTC, so it is not in *created since the 4th*; in `FAR_EAST` the 4th began
	at 10:00 UTC on the 3rd, so it is. One row, two answers, and which is right depends entirely
	on which zone reached the boundary.

	**Both steps below the explicit one are driven, and finding out why cost a falsification.**
	The first version moved the *workspace* only — and `bootstrap` gives the user a timezone
	too, which wins, so the workspace could never have been reached and removing it from the
	chain changed nothing. A test of a precedence rule has to clear the levels above the one it
	is asking about. `#773` is what this looks like once it reaches somebody: correct in winter,
	wrong in summer.
	"""

	assert world.titles("/v1/tasks?created_at.gte=2026-08-04") == ["the 5th"]

	# The workspace's, with the user stating none — §6.5's third step.
	world.user.timezone = None
	world.workspace.timezone = FAR_EAST
	world.session.flush()

	assert world.titles("/v1/tasks?created_at.gte=2026-08-04") == ["the 3rd", "the 5th"]

	# And the user's own beats it, which is the step above.
	world.user.timezone = INSTANCE_ZONE
	world.session.flush()

	assert world.titles("/v1/tasks?created_at.gte=2026-08-04") == ["the 5th"]

	# **The instance is the last word, and this step needed its own case for the same reason
	# the workspace did.** `zone_for` falls back to UTC when it is handed nothing, so an
	# instance whose zone *is* UTC cannot be told apart from one that was never passed —
	# dropping it from the chain changed no answer until this line existed.
	world.user.timezone = None
	world.workspace.timezone = None
	_instance(world.session).timezone = FAR_EAST
	world.session.flush()

	assert world.titles("/v1/tasks?created_at.gte=2026-08-04") == ["the 3rd", "the 5th"]


def test_a_misspelled_field_is_refused_rather_than_ignored (world: World) -> None:
	"""The property the whole seam exists for: nothing dotted is quietly dropped."""

	answer = world.call("GET", "/v1/tasks?creatd_at.gte=2026-08-01")

	assert answer.status_code == 422, answer.text
	assert "creatd_at" in answer.text
	assert "created_at" in answer.text, "the refusal did not name the fields that do exist"


def test_equality_on_a_timestamp_is_refused_over_the_wire (world: World) -> None:
	"""Simon's decision of 2026-08-11, reaching a caller rather than only the compiler."""

	answer = world.call("GET", "/v1/tasks?created_at.eq=2026-08-03")

	assert answer.status_code == 422, answer.text
	assert "created_at.gte" in answer.text, "the refusal did not say what to write instead"


def _readerless () -> list[str]:
	"""Return every `/v1` collection this application serves that declares no filter reader.

	**Derived rather than named, because the first version named one and outlived it**
	(`#1431`). It drove `/v1/changes`, which was the obvious example of a listing that could not
	filter on dates — and then `/v1/changes` grew a reader, so the guard was asserting the
	opposite of what the route now does. A test that names its subject is orphaned the day
	somebody changes it, and this one would have failed loudly rather than silently only by
	luck.

	**Walking the routers rather than the built application**, which is `#37`'s recorded trap:
	`include_router` leaves a private wrapper in `app.routes` with no path at all, so a check
	that walks the running app sees nothing and reports clean.
	"""

	found = []

	for prefix, router in subroutine.api.app.ROUTERS:
		for route in router.routes:
			path = prefix + getattr(route, "path", "")

			if "GET" not in (getattr(route, "methods", set()) or set()):
				continue

			# Collections only: a path parameter needs a real id to drive, and a single-entity
			# read is deliberately outside this seam anyway.
			if "{" in path or not path.startswith("/v1"):
				continue

			if subroutine.api.filters.declared_by(route) is None:
				found.append(path)

	return sorted(found)


def test_the_routes_that_declare_a_reader_are_the_ones_that_should (world: World) -> None:
	"""The population the guard below reads, checked for being neither empty nor everything.

	**A floor, because a derivation that returns nothing passes every test built on it** — and
	the walk above has a recorded way of returning nothing, since `include_router` hides its
	routes behind a wrapper. Without this, breaking the walk would make the seam look policed
	when nothing was being driven at all.
	"""

	readerless = _readerless()

	assert len(readerless) > 3, f"the walk found almost nothing, so it is probably broken: {readerless}"
	assert "/v1/tasks" not in readerless, "the task listing lost its date filters"
	assert "/v1/changes" not in readerless, (
		"the change feed lost its date filters — SR#1431 gave it one, and the guard below "
		"used to name it as the example of a route without"
	)


@pytest.mark.parametrize("path", _readerless())
def test_a_listing_that_declares_no_reader_still_refuses_a_dotted_name (
	world: World, path: str
) -> None:
	"""**The other half of the seam, and the one that could fail silently.**

	`refuse_unknown` stops policing dotted names *only* where a route declares a reader. If it
	stopped policing them everywhere, a date filter sent to a listing that cannot honour one
	would be ignored and answered `200` — a complete, plausible, wrong answer, which is exactly
	the failure that module was written to prevent.

	**Every such route rather than one**, so this cannot be aimed at a route that later gains a
	reader and quietly stop testing the property.
	"""

	answer = world.call("GET", f"{path}?created_at.gte=2026-08-01")

	assert answer.status_code == 422, f"{path} did not refuse a filter it cannot honour: {answer.text}"
	assert "created_at.gte" in answer.text, f"{path} refused without naming what it refused"


def test_documents_answer_the_same_question (world: World) -> None:
	"""One ref counter serves both kinds (§6.2), so half the numbers are documents.

	*What was created yesterday* answered for tasks alone would be wrong about half of what a
	ref can name — which is why the registry has a document entry at all.
	"""

	created = world.call(
		"POST", "/v1/documents", json={"title": "A conclusion", "body": "."}
	)

	assert created.status_code == 201, created.text
	assert world.titles("/v1/documents?created_at.gte=now-1y") == ["A conclusion"]
	assert world.titles("/v1/documents?created_at.lt=2026-01-01") == []


def test_a_listing_is_narrowed_by_rank_by_reference_and_by_whether_a_field_is_set (
	world: World,
) -> None:
	"""**`SR#1804`'s four questions, driven** — design `SR#1801`, and Simon's own examples.

	| asked | before |
	| --- | --- |
	| urgency ≥ 3 | **422** — not a field this endpoint can filter on |
	| not a sub-task | **404** — ``parent=none`` looked up a task called *none* |
	| unassigned | **404** — the same, for an account |
	| two tags at once | **422** — *'tag' takes one value and was given 2* |

	Every one was a gap in the *vocabulary* rather than in the architecture: `SR#1801` §2 drove
	search, filters, ordering and grouping together and they compose today. What was missing is
	what a caller may name.

	**`is` takes a condition and `eq` takes a value**, which is the line `SR#1801` §5 draws and
	the reason ``is`` must never become GitHub's grab-bag. Both are asserted here on one field,
	because the pair is the claim.
	"""

	ordinary = world.call(
		"POST", "/v1/tasks", json={"title": "Ordinary", "urgency": 2, "importance": 2}
	)
	urgent = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "Urgent", "urgency": 5, "importance": 4, "tags": ["ops"]},
	)
	somebody_elses = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "Somebody has this", "assignee": str(world.user.username),
			"tags": ["web"]},
	)

	assert {one.status_code for one in (ordinary, urgent, somebody_elses)} == {201}

	# **A rank, which was sortable and unaskable** — the finding that decided the registry.
	assert world.titles("/v1/tasks?urgency.gte=3") == ["Urgent"]
	assert world.titles("/v1/tasks?importance.eq=4") == ["Urgent"]

	# **A condition, on a field that has one.** `assignee=none` used to be a 404 about an
	# account called *none*.
	unassigned = world.titles("/v1/tasks?assignee.is=unset")

	assert "Somebody has this" not in unassigned and "Urgent" in unassigned
	assert world.titles("/v1/tasks?assignee.is=set") == ["Somebody has this"]

	# **And the value, through the same resolver the flat spelling uses**, so the dotted form
	# takes a username rather than being a second and narrower door onto the same column.
	assert world.titles(
		f"/v1/tasks?assignee.eq={world.user.username}"
	) == ["Somebody has this"]

	# **Either of two tags**, which had no spelling at all — `tag` took one value and refused
	# two by name.
	assert sorted(world.titles("/v1/tasks?tag.in=ops,web")) == [
		"Somebody has this", "Urgent"
	]
	assert world.titles("/v1/tasks?tag.eq=ops") == ["Urgent"]

	# **Two entries about one field are ANDed**, which is how a caller asks for *both* — and
	# nothing here carries both tags, so the answer is empty rather than either.
	assert world.titles("/v1/tasks?tag.eq=ops&tag.eq=web") == []

	# **`is` is refused where a column cannot be null**, because it could only ever answer all
	# or nothing. `SR#1804` — the guard above found this in the first version of it.
	refused = world.call("GET", "/v1/tasks?created_at.is=unset")

	assert refused.status_code == 422, refused.text

	# **And the two reserved words are the whole vocabulary**, which is what keeps `is` from
	# becoming a place to put anything that reads like a state.
	nonsense = world.call("GET", "/v1/tasks?assignee.is=somebody")

	assert nonsense.status_code == 422, nonsense.text
	assert "set or unset" in nonsense.text


def _an_agent_of (world: World, name: str) -> subroutine.db.models.identity.User:
	"""Return a live service account answerable to ``world.user`` and able to hold work.

	**Created through the domain rather than the route**, because ``POST /v1/users`` takes no
	``responsible`` — accountability is *inherited from the creator* (`SR#473`), and an actor is
	how that is expressed. The membership is separate and is needed: resolving a name for a
	*filter* spans the instance (`SR#501`), where assigning work does not.
	"""

	agent = subroutine.domain.users.create(
		session=world.session,
		username=name,
		is_service_account=True,
		actor=subroutine.domain.authentication.Principal(user=world.user, token=None),
	)
	subroutine.domain.workspaces.add_member(
		world.session, world.workspace, agent, role_key="member"
	)
	world.session.flush()

	return agent


def test_a_listing_answers_what_a_persons_agents_are_holding (world: World) -> None:
	"""**`SR#848`, and it is the read half of `SR#473`'s model** — Simon's decision, 2026-09-06.

	Handing work *to* an agent has worked since M1 and the accountability chain is walked on
	every authenticated request. Asking *what came of it* reached nothing at all: ``assignee``
	names **one** account, so *mine and my agents'* meant fetching a roster and joining it in
	whichever client wanted the answer — which is a second copy of the chain rule, refused for
	`SR#925`'s reason on `SR#1420`.

	**The caller is in their own set**, which is the difference between this and `SR#518`.
	``--assignee me`` shipped *assigned to me*; this is *mine and theirs*, and leaving the person
	out would answer a question nobody asked and cost two requests to ask the one they did.
	"""

	agent = _an_agent_of(world, f"agent-{uuid.uuid4().hex[:8]}")
	stranger = subroutine.domain.users.create(
		session=world.session, username=f"other-{uuid.uuid4().hex[:8]}"
	)
	subroutine.domain.workspaces.add_member(
		world.session, world.workspace, stranger, role_key="member"
	)
	world.session.flush()

	for title, holder in (
		("Mine", world.user.username),
		("My agent's", agent.username),
		("Somebody else's", stranger.username),
	):
		made = world.call("POST", "/v1/tasks", json={"title": title, "assignee": str(holder)})

		assert made.status_code == 201, made.text

	assert world.titles(f"/v1/tasks?answers_to.eq={world.user.username}") == [
		"Mine", "My agent's"
	]

	# **And it is narrower than the listing**, which is what says the filter ran at all: the
	# fixture's own three tasks are unassigned and the stranger's is held by somebody outside
	# the chain, so none of the four appears above.
	assert "Somebody else's" in world.titles("/v1/tasks")

	# **A person with no agents gets their own work and no more**, so the set is resolved rather
	# than waved at. This is the case that fails if the walk is dropped and everybody is
	# returned.
	assert world.titles(f"/v1/tasks?answers_to.eq={stranger.username}") == ["Somebody else's"]


def test_whose_responsibility_walks_the_chain_rather_than_one_hop (world: World) -> None:
	"""An agent's own agent is answerable to the person at the end — `SR#473`, `SR#848`.

	``agents_answering_to`` walks outward level by level and this is the caller that makes the
	difference visible: a one-hop implementation passes every assertion in the case above and
	fails here, which is why the two are separate.
	"""

	agent = _an_agent_of(world, f"agent-{uuid.uuid4().hex[:8]}")
	agent.is_superuser = True
	world.session.flush()

	sub = subroutine.domain.users.create(
		session=world.session,
		username=f"sub-{uuid.uuid4().hex[:8]}",
		is_service_account=True,
		actor=subroutine.domain.authentication.Principal(user=agent, token=None),
	)
	subroutine.domain.workspaces.add_member(
		world.session, world.workspace, sub, role_key="member"
	)
	world.session.flush()

	made = world.call(
		"POST", "/v1/tasks", json={"title": "Two hops away", "assignee": str(sub.username)}
	)

	assert made.status_code == 201, made.text
	assert world.titles(f"/v1/tasks?answers_to.eq={world.user.username}") == ["Two hops away"]


def test_asking_whose_responsibility_names_an_account_that_does_not_exist (
	world: World,
) -> None:
	"""A name nobody holds is refused by name, never answered with an empty page — §7.3a.

	The same resolver the flat ``?assignee=`` uses, which is what keeps this from being a second
	and narrower door onto one column.
	"""

	refused = world.call("GET", "/v1/tasks?answers_to.eq=nobody-at-all")

	assert refused.status_code == 404, refused.text
	assert "nobody-at-all" in refused.text


def test_whose_responsibility_offers_no_condition_of_its_own (world: World) -> None:
	"""``answers_to.is`` is refused, because ``assignee.is`` is already that question — `SR#848`.

	**One question with two spellings is what this refuses**, and the narrower one would lie
	about its subject: *unset* on this field could only mean *has no assignee at all*, which says
	nothing about anybody's responsibility. `SR#1804` drew the line between a field's **value**
	and its **condition**; this keeps the condition where the column is.
	"""

	refused = world.call("GET", "/v1/tasks?answers_to.is=unset")

	assert refused.status_code == 422, refused.text
	assert "answers_to.is" not in subroutine.domain.filtering.names("task")
	assert "assignee.is" in subroutine.domain.filtering.names("task")


def test_a_tag_cannot_be_named_in_a_way_a_filter_could_not_ask_for (
	world: World,
) -> None:
	"""A comma in a tag's name, refused — `SR#1804`, Simon's decision of 2026-09-01.

	``tag.in=ops,web`` narrows to either of two tags, so a comma inside a name would make *one
	tag called "ops,web"* and *two tags* the same string — an ambiguity a filter cannot resolve
	and a caller cannot escape.

	**The cost was measured before the rule**: the `projects` workspace holds 34 tags and not
	one contains a comma. It sits beside the all-digits rule, which exists for the same kind of
	reason — a name that could not be written in the syntax that names it.
	"""

	refused = world.call(
		"POST", "/v1/tasks", json={"title": "Tagged oddly", "tags": ["ops,web"]}
	)

	assert refused.status_code == 422, refused.text
	assert "two tags" in refused.text

	# **Enforced where every tag passes, not in the parser** — `SR#1167`'s finding, which is
	# that renaming is a second door and the digit rule was missed at it for as long as it
	# existed.
	made = world.call("POST", "/v1/tasks", json={"title": "Tagged", "tags": ["ops"]})

	assert made.status_code == 201, made.text


#: A tag the case above makes before it drives a filter that names one.
_A_TAG = "ops"

#: An item that exists, for the filters whose value is a ref rather than a name — `SR#1829`.
#:
#: **The first of the fixture's three**, in a fresh workspace where refs start at 1 (§6.2).
_A_REF = "1"

#: The ref the case below gives the document it makes, so a document's tree filters are driven
#: with a *document* — `SR#2173`. The fixture makes three tasks, so the fourth number is the
#: first document, and asking a document listing about a task's ref is refused by name.
_A_DOCUMENT_REF = "4"

def _default_status (entity_type: str) -> str:
	"""Return the status something of this kind gets when nobody says.

	**Derived from the seeds rather than spelled**, beside ``seed.default_type`` which already
	answers the other half of the same question. A task's default is ``open`` and a document's
	is ``draft``, and writing either here would be a second copy of a table this world is
	actually built from.
	"""

	return next(
		seed.key
		for seed in subroutine.db.seed.SEEDED_STATUSES
		if seed.entity_type == entity_type and seed.is_default
	)


#: What each `REFERENCE` field takes, since one kind covers several vocabularies — `SR#1804`.
#:
#: **Per field rather than per kind**, which is where this map differs from :data:`_SAMPLES`
#: below and has to: a username, a tag name and a project key are three vocabularies wearing one
#: kind, and driving a tag filter with a username reports the route as broken.
#:
#: **And per entity since `SR#1829`**, because ``status`` and ``type`` are a *workspace's* own
#: vocabulary scoped by entity: a task's statuses and a document's are different rows in one
#: table, so ``status.eq=open`` is right on one listing and refused on the other. Keeping this
#: keyed by field alone would have driven a document's status filter with ``open`` and reported
#: a working route as broken — which is the failure the comment above :data:`_SAMPLES` records
#: one axis along, and which this map's own comment predicted in as many words.
_REFERENCES: dict[tuple[str, str], str] = {
	("task", "tag"): _A_TAG,
	("document", "tag"): _A_TAG,
	("task", "status"): _default_status("task"),
	("document", "status"): _default_status("document"),
	("task", "type"): subroutine.db.seed.default_type("task"),
	("document", "type"): subroutine.db.seed.default_type("document"),
	# **The Inbox, because it is the one project every workspace is guaranteed to have** —
	# `workspaces.create` makes it (`SR#301`) and a workspace without one refuses every task
	# filed with no project. Any other key would be a fixture this map cannot see.
	("task", "project"): subroutine.domain.workspaces.INBOX_KEY,
	("document", "project"): subroutine.domain.workspaces.INBOX_KEY,
	# **A ref, which is a fourth vocabulary wearing this one kind** — `SR#1829`. Driven with a
	# username, `parent.eq` answered *there is no task 'si-7c09b9c3'*, which is the route working
	# correctly and reads as a broken one. The comment above predicted exactly this.
	#
	# **`#1` because the fixture makes its three tasks in a fresh workspace**, where refs start
	# at 1 (§6.2) — an assumption this file already rests on, since two cases above complete
	# `/v1/tasks/1`.
	("task", "parent"): _A_REF,
	("task", "under"): _A_REF,
	# **A document's ref, and it must be a *document*** — `SR#2173`. One counter numbers both
	# kinds (§6.2), so `#1` is a task here and `documents.parent.eq=1` is refused *by name*:
	# "1 is a task, not a document". The case below makes one before it drives these.
	("document", "parent"): _A_DOCUMENT_REF,
	("document", "under"): _A_DOCUMENT_REF,
}


@pytest.mark.parametrize("entity", sorted(subroutine.domain.filtering.FILTERS))
def test_every_published_filter_is_accepted_by_the_listing_that_publishes_it (
	world: World, entity: str
) -> None:
	"""**Derived from the registry, so a new field is driven the day it is declared.**

	`/v1/meta` is where an agent reads what it may send, and a published name the route refuses
	is a contract nothing enforces — this codebase's recurring defect, and one that would be
	invisible here because both halves would look right in isolation.

	Every combination is *driven* rather than compared against a list, since the question is
	whether the route answers, not whether two strings match.
	"""

	# **A tag has to exist before a filter can name one**, because `tags.carrying` refuses a
	# name nobody uses rather than answering with an empty listing (`SR#1319`). So the fixture
	# for a `REFERENCE` sample is made here, on the entity being driven.
	if entity in ("task", "document"):
		world.call(
			"POST",
			"/v1/tasks" if entity == "task" else "/v1/documents",
			json={"title": "Something tagged", "tags": [_A_TAG]},
		)

	published = world.call("GET", "/v1/meta").json()["listings"][entity]["filters"]
	dotted = sorted(name for name in published if "." in name)

	assert dotted, f"{entity} publishes no dotted filters"
	assert dotted == sorted(subroutine.domain.filtering.names(entity))

	for name in dotted:
		# **The value follows the field's kind**, read from the registry rather than fixed —
		# `touched_by` takes a username, and driving every combination with `today` refused it
		# with *there is no account called 'today'*, which is the route working correctly.
		field, _, operator = name.partition(".")
		kind = subroutine.domain.filtering.FILTERS[entity][field].kind
		value = _sample(kind, operator, field, world, entity=entity)

		answer = world.call("GET", f"{published_path(published, entity)}?{name}={value}")

		assert answer.status_code == 200, f"{name} is published and refused: {answer.text}"


#: A value each kind of filter can actually read.
#:
#: **A map with a completeness check rather than a chain of conditions** (`#319`). This was an
#: if/else on `WHO`, so a third kind fell through to `today` — and `estimate_minutes.lte=today`
#: is refused, which arrives looking like a broken route rather than like a test that has not
#: been told about a new kind. The failure below names the real problem instead.
_SAMPLES: dict[str, str] = {
	"INSTANT": "today",
	"DURATION": "2h",
	"NUMBER": "3",
	# **`CONDITION` has no value of its own**, which is what the kind *is*: its only operator is
	# `is`, and that is answered above before a kind is consulted. It is here so the
	# completeness check below stays a real one — a kind absent from this map is a kind nobody
	# has thought about, and leaving this one out would make that indistinguishable.
	"CONDITION": subroutine.domain.filtering.UNSET,
	# **`REFERENCE` is answered above, per field** — see :data:`_REFERENCES`. Here so the
	# completeness check stays real: a kind absent from this map is one nobody has thought
	# about, and leaving this one out would make that indistinguishable.
	"REFERENCE": _A_TAG,
	# **`ANSWERABLE` is answered above too**, because it needs an account this instance really
	# has. Here so the completeness check stays real — `SR#848`.
	"ANSWERABLE": "",
}


def _sample (
	kind: subroutine.domain.filtering.Kind,
	operator: str,
	field: str,
	world: World,
	*,
	entity: str,
) -> str:
	"""Return something this filter will accept, given its kind and its operator.

	**The operator is asked first, and it has to be** (`SR#1804`). ``is`` takes one of two
	reserved words whatever the field holds, so a value chosen by kind alone drove
	``created_at.is=today`` and reported four routes as broken — which is the same shape the
	comment below records for `SR#319`, one axis along. A kind says what a *value* looks like;
	``is`` does not take one.
	"""

	if operator == subroutine.domain.filtering.IS:
		return subroutine.domain.filtering.UNSET

	if kind is subroutine.domain.filtering.WHO:
		return str(world.user.username)

	# **A username, like `WHO`, and answered here rather than in the map for the same reason**:
	# the value has to be an account this instance really has, which only `world` knows. `SR#848`.
	if kind is subroutine.domain.filtering.ANSWERABLE:
		return str(world.user.username)

	if kind is subroutine.domain.filtering.REFERENCE:
		# **An account is the default and the exceptions are named**, because every reference
		# but `tag` resolves a username today — and a field added to that kind with a
		# vocabulary of its own would be driven with a username and report the route broken,
		# which is the failure the comment above `_SAMPLES` records for kinds.
		return _REFERENCES.get((entity, field), str(world.user.username))

	for name, value in _SAMPLES.items():
		if kind is getattr(subroutine.domain.filtering, name):
			return value

	raise AssertionError(
		f"no sample value for a filter of this kind ({kind.expects!r}). Add one to _SAMPLES, "
		f"or the case above will drive it with something it cannot read and report the route "
		f"as broken."
	)


def published_path (published: list[str], entity: str) -> str:
	"""Return the listing path for an entity, so the case above reads as one question.

	**Read off `meta.LISTINGS` rather than spelled here** (`#1431`). This was a literal of three
	entities and went stale the moment a fourth was published — which made the guard fail with a
	`KeyError` about its own map rather than a statement about the route, and the obvious repair
	was to add a line to a second copy of a list the application already declares.
	"""

	for published_entity, path, _sortable, _selectable in subroutine.api.meta.LISTINGS:
		if published_entity == entity:
			return path

	raise AssertionError(
		f"{entity!r} publishes filters and `meta.LISTINGS` does not name it, which should be "
		f"impossible: this parametrisation reads that same table."
	)


def test_asking_when_something_was_completed_reaches_finished_work (
	world: World,
) -> None:
	"""**`#818`** — Simon's second question, which answered `[]` until the two rules met.

	A listing hides finished work unless asked, and `completed_at` is null on everything that
	is not finished. So the one field whose every value belongs to a finished task was compared
	against a set with all of them already filtered out.

	The precedent is exact and one spelling along: `tasks.completion_wanted` records that
	`?status_category=done` answering `[]` on an instance full of finished work is *a plausible,
	complete, wrong answer*. This is that request, differently written.
	"""

	answer = world.call("POST", "/v1/tasks/1/complete")

	assert answer.status_code == 200, answer.text
	assert world.titles("/v1/tasks?completed_at.gte=now-1y") == ["the 1st"]

	# **And nothing else widens.** The implication belongs to the field being asked about, so a
	# filter on `created_at` hides finished work exactly as before — a listing that grew every
	# time it was asked about a date would be the same defect facing the other way.
	assert "the 1st" not in world.titles("/v1/tasks?created_at.gte=now-1y")


def test_asking_about_completion_and_excluding_it_is_refused (world: World) -> None:
	"""A contradiction is named rather than settled in one parameter's favour.

	There is no reading of *work finished yesterday, and no finished work* that means anything,
	and the refusal is the same one a finished `status_category` already gets.
	"""

	answer = world.call(
		"GET", "/v1/tasks?completed_at.gte=now-1y&include_completed=false"
	)

	assert answer.status_code == 422, answer.text
	assert "completed_at" in answer.text
	assert "include_completed" in answer.text


def test_a_comment_counts_as_having_worked_on_something (world: World) -> None:
	"""**Simon's third question, and the whole reason this is an `EXISTS`** — `#815`, `#817`.

	A comment does not move the commented-on item's `updated_at`. Measured on the live
	instance: identical to the microsecond. So a filter built on the row's own timestamps would
	answer *what did I work on yesterday* **wrongly rather than partially**, and nothing in the
	answer would say which.

	The task named here was created on the 1st and has not been edited since. It appears only
	because somebody commented on it today.
	"""

	assert world.titles("/v1/tasks?updated_at.gte=today") == []

	commented = world.call("POST", "/v1/tasks/1/comments", json={"body": "Looked at it."})

	assert commented.status_code == 201, commented.text

	# Still nothing by the row's own clock, which is the measurement this rests on.
	assert world.titles("/v1/tasks?updated_at.gte=today") == []
	assert world.titles("/v1/tasks?touched_at.gte=today") == ["the 1st"]


def test_activity_answers_for_a_period_rather_than_a_moment (world: World) -> None:
	"""Simon's fourth question: *what has been worked on since the 2nd August*."""

	assert world.titles("/v1/tasks?touched_at.gte=2026-08-02") == ["the 3rd", "the 5th"]
	assert world.titles(
		"/v1/tasks?touched_at.gte=2026-08-02&touched_at.lt=2026-08-05"
	) == ["the 3rd"]


def test_claiming_something_is_not_working_on_it (world: World) -> None:
	"""Decision `#817`: a lease is bookkeeping, and `#726` records the case it misreports.

	Somebody may claim an item to *read* it and decide it is not for them, and then nothing was
	ever worked on. Written as an exclusion rather than a list of what counts, so an action
	added later is included by default — too many rows rather than work silently missing.
	"""

	assert world.titles("/v1/tasks?touched_at.gte=today") == []

	claimed = world.call("POST", "/v1/tasks/1/claim")

	assert claimed.status_code == 200, claimed.text
	assert world.titles("/v1/tasks?touched_at.gte=today") == []

	released = world.call("POST", "/v1/tasks/1/release")

	assert released.status_code == 200, released.text
	assert world.titles("/v1/tasks?touched_at.gte=today") == []


def test_whose_activity_and_when_are_one_question (world: World) -> None:
	"""**One correlated `EXISTS`, not two predicates** — decision `#817`.

	Compiled independently they would mean *some event in the window* and *some event by si*,
	possibly different ones — so an item somebody else touched today and si touched last week
	would answer *what did si work on today*. This is the case that tells the two apart.
	"""

	world.call("POST", "/v1/tasks/1/comments", json={"body": "Looked at it."})

	assert world.titles(
		f"/v1/tasks?touched_at.gte=today&touched_by.eq={world.user.username}"
	) == ["the 1st"]

	# The 3rd was created on the 3rd by this same person, and not touched today. Asking for
	# both together must not find it — two independent predicates would.
	assert "the 3rd" not in world.titles(
		f"/v1/tasks?touched_at.gte=today&touched_by.eq={world.user.username}"
	)


def test_asking_who_touched_it_names_an_account_that_does_not_exist (
	world: World,
) -> None:
	"""A username is resolved rather than matched, so a typo is refused instead of matching none."""

	answer = world.call("GET", "/v1/tasks?touched_by.eq=nobody")

	assert answer.status_code == 404, answer.text
	assert "nobody" in answer.text


def test_not_touched_by_is_refused_rather_than_answered_ambiguously (
	world: World,
) -> None:
	"""`ne` on `touched_by` reads as two different questions, so it is refused by name.

	Inside one `EXISTS` it means *there is an event here somebody else wrote*, which is true of
	anything two people have touched — not *this was not touched by them*. A filter with two
	readings and one answer is the shape decision `#817` refused for `eq` on a timestamp.
	"""

	answer = world.call("GET", f"/v1/tasks?touched_by.ne={world.user.username}")

	assert answer.status_code == 422, answer.text
	assert "touched_by" in answer.text


def test_asking_what_was_worked_on_reaches_what_was_finished (world: World) -> None:
	"""**Simon's third question names *completed* among the things that count** — `#815`.

	A listing hides finished work unless asked, so *what did I work on today* left out the one
	task that was completed today — which is the item you most want to see when you ask. Found
	by driving all five questions on a real instance rather than by reading: it was the only
	row absent, and an absence is what nobody checks.

	Decision `#817` settles the direction: the failure this filter must not have is work that
	is silently missing.
	"""

	assert world.call("POST", "/v1/tasks/1/complete").status_code == 200
	assert "the 1st" in world.titles("/v1/tasks?touched_at.gte=now-1y")


def test_working_on_something_unfinished_is_a_question_you_may_still_ask (
	world: World,
) -> None:
	"""And this is where it parts company with `completed_at` — `#818` refuses, this obeys.

	*What did I work on today that is not finished yet* is an ordinary question, so saying no
	is honoured rather than refused. Beside `completed_at` the same words ask for finished work
	and no finished work, which means nothing and is turned down by name.
	"""

	assert world.call("POST", "/v1/tasks/1/complete").status_code == 200

	answer = world.call(
		"GET", "/v1/tasks?touched_at.gte=now-1y&include_completed=false"
	)

	assert answer.status_code == 200, answer.text
	assert "the 1st" not in [item["title"] for item in answer.json()["items"]]


def test_a_listing_answers_what_is_short (world: World) -> None:
	"""`#319`, and the half there was no way to express at all.

	``~2h`` is one of four things the capture grammar reads off a line, it is rendered by
	three surfaces and published in ``/v1/meta`` — so people are asked to supply it and it
	then answered no question. `#251`'s shape: collected, displayed, and read by nothing that
	decides anything.

	**§6.4's grammar, through ``durations.parse``**, so ``2h`` means here exactly what ``~2h``
	means in a captured line. Driven with all three spellings of the same length, because a
	filter that took only the bare number would be a second grammar for one value.
	"""

	world.call("POST", "/v1/tasks", json={"text": "Quick one ~20m"})
	world.call("POST", "/v1/tasks", json={"text": "Medium ~2h"})
	world.call("POST", "/v1/tasks", json={"text": "Long one ~3d"})
	world.call("POST", "/v1/tasks", json={"title": "Unestimated"})

	for spelling in ("2h", "120", "1h30m"):
		answer = world.call("GET", f"/v1/tasks?estimate_minutes.lte={spelling}")

		assert answer.status_code == 200, answer.text

		titles = {row["title"] for row in answer.json()["items"]}
		expected = {"Quick one", "Medium"} if spelling != "1h30m" else {"Quick one"}

		assert titles == expected, f"{spelling} selected {titles}"

	# **The unestimated are not "short".** Absent from every comparison, which is what a null
	# means in SQL and is also the honest answer: nobody has said how long it takes.
	both = world.call("GET", "/v1/tasks?estimate_minutes.gte=0").json()["items"]

	assert "Unestimated" not in {row["title"] for row in both}


def test_the_question_the_item_was_filed_for (world: World) -> None:
	"""*Not blocked, and small* — asked for by Simon on 2026-08-02 and unanswerable until now.

	``--ready`` answered the first half from the beginning and there was no way to say the
	second on any surface. Driven as one request because that is how it was asked.
	"""

	world.call("POST", "/v1/tasks", json={"text": "Quick and free ~20m"})
	world.call("POST", "/v1/tasks", json={"text": "Quick but big ~3d"})

	blocked = world.call("POST", "/v1/tasks", json={"text": "Quick but blocked ~15m"}).json()
	blocker = world.call("POST", "/v1/tasks", json={"title": "In the way"}).json()

	world.call(
		"POST",
		f"/v1/tasks/{blocker['ref']}/links",
		json={"target": blocked["ref"], "link_type": "blocks", "target_type": "task"},
	)

	answer = world.call(
		"GET", "/v1/tasks?ready=true&estimate_minutes.lte=1h&order=estimate_minutes"
	)

	assert answer.status_code == 200, answer.text
	assert [row["title"] for row in answer.json()["items"]] == ["Quick and free"]


def test_a_length_that_cannot_be_read_is_refused_in_its_own_words (world: World) -> None:
	"""**The refusal says what the field takes, and it used to say what a date takes.**

	`_unreadable` answered *"does not say when"* and pointed at `relative_dates`, which was
	true of every filterable field there was until an estimate became one — so a caller writing
	``estimate_minutes.lte=fortnight`` would have been given the date grammar. One of a thing,
	in a refusal.
	"""

	answer = world.call("GET", "/v1/tasks?estimate_minutes.lte=fortnight")

	assert answer.status_code == 422, answer.text

	reported = answer.json()["errors"][0]

	assert reported["field"] == "estimate_minutes"
	assert "30m" in reported["message"], "it has to say what a length looks like"
	assert "relative_dates" not in (reported["hint"] or ""), "and not what a date looks like"


def _dated (world: World) -> None:
	"""Give the three tasks deadlines on the days they are named after.

	The fixture dates them by ``created_at`` alone, because that is what `#815`'s five questions
	are about. `#1017` is about the *deadline* pair, which needs a column nothing else here
	sets — so it is set per test rather than in the fixture, where it would silently change what
	every other case is asking.
	"""

	for title, when in DAYS.items():
		world.session.execute(
			sqlalchemy.update(subroutine.db.models.work.Task)
			.where(subroutine.db.models.work.Task.title == title)
			.values(due_at=when, due_is_all_day=False)
		)

	world.session.flush()


def test_a_bare_date_reaches_the_older_spelling_of_a_deadline_bound (world: World) -> None:
	"""`#1017`. ``?due_after=2026-08-18`` was a **500** on the served instance.

	`due_before` and `due_after` are the only two ``datetime.datetime`` query parameters in the
	whole API, so they are the one shape §9.6's grammar could not protect. Pydantic reads a bare
	date as a *naive* datetime, which `db/types.UtcDateTime` refuses on the way to the column —
	correctly, and at execute time, where a refusal becomes ``internal_error`` and the caller is
	told nothing about the parameter they sent.

	**Asserted against the newer spelling rather than against a literal list**, which is the
	whole point of the fix: the two are one implementation now, so a change to the boundary rule
	cannot move one and leave the other. A hand-written expectation would pass while they
	diverged.
	"""

	_dated(world)

	assert world.titles("/v1/tasks?due_after=2026-08-03") == ["the 5th"]
	assert world.titles("/v1/tasks?due_after=2026-08-03") == world.titles(
		"/v1/tasks?due_at.gt=2026-08-03"
	)

	assert world.titles("/v1/tasks?due_before=2026-08-03") == ["the 1st"]
	assert world.titles("/v1/tasks?due_before=2026-08-03") == world.titles(
		"/v1/tasks?due_at.lt=2026-08-03"
	)


def test_the_older_spelling_still_takes_the_instant_it_always_did (world: World) -> None:
	"""What the fix must not break: a full timestamp is what every existing caller sends.

	`#1017` widens these two from an instant to *whatever the caller supplied*, which is the
	same widening `due_at.gt` already has. The ISO form has always worked and is the only form
	anything in the wild is using, so it is asserted rather than assumed.
	"""

	_dated(world)

	assert world.titles("/v1/tasks?due_after=2026-08-03T12:00:00Z") == ["the 5th"]
	assert world.titles("/v1/tasks?due_before=2026-08-03T12:00:00Z") == ["the 1st"]


def test_the_older_spelling_reaches_the_expression_grammar_too (world: World) -> None:
	"""And it gains what it never had: the vocabulary `/v1/meta` publishes.

	`?due_before=start_of_week+3d` is the case `#815` fixed for the dotted spelling and left
	broken for this one — a 422 about an invalid character in a year, for a grammar the same
	instance advertises.
	"""

	_dated(world)

	# **Two expressions, because one cannot fail.** The first version asserted only that
	# `now-1y` answered 200 with three rows — and a parameter that is *ignored* answers exactly
	# that, so it survived the falsification that emptied `ALIASES`. Every deadline here is in
	# the past, so `now` must exclude all three, and only a filter that was genuinely applied
	# can tell the two apart.
	assert world.titles("/v1/tasks?due_after=now-1y") == ["the 1st", "the 3rd", "the 5th"]
	assert world.titles("/v1/tasks?due_after=now") == []


def test_a_deadline_bound_that_cannot_be_read_names_the_field (world: World) -> None:
	"""A 422 naming the parameter, never a 500 — which is the half of `#1017` that is not a widening.

	Whichever way the boundary question had been settled, a value the program cannot read is a
	fault in the request and has to be reported as one. ``internal_error`` sends the reader to
	their own configuration, which is `#377`'s recorded cost in a different module.
	"""

	answer = world.call("GET", "/v1/tasks?due_after=whenever")

	assert answer.status_code == 422, answer.text
	assert answer.json()["errors"][0]["field"] == "query.due_after", "it names what they sent"


def test_an_alias_resolves_to_the_field_it_is_a_synonym_for () -> None:
	"""`#1017`. The half of the fix nothing on the wire can currently reach.

	`Asked.about` decides whether a listing reaches finished work, and it used to read each
	comparison's name *as written* and partition it on the separator. An alias carries no
	separator, so `due_after` would have been compared against `due_at` and answered no — a
	filter that was applied and invisible to the rule that reads it.

	**No alias is on a completion field today, so nothing over HTTP can show this.** That is
	exactly why it is asserted here rather than left as a defensive edit: an unreachable
	correctness fix with no test is indistinguishable from one that does nothing, which is the
	shape this project keeps finding.
	"""

	resolved = subroutine.domain.filtering.understood(
		[("due_after", "2026-08-03")], entity="task"
	)

	assert len(resolved) == 1, "the alias is read even though it carries no separator"
	assert resolved[0].field == "due_at", "and resolves to the field it is a synonym for"
	assert resolved[0].operator == "gt", "with the operator that decides its boundary"
	assert resolved[0].name == "due_after", "while still remembering what the caller wrote"

	asked = subroutine.api.filters.Asked(entity="task", comparisons=resolved)

	assert asked.about("due_at"), "so the listing knows which column was asked about"


def test_a_flat_name_that_is_not_an_alias_is_still_left_alone () -> None:
	"""The other direction, which is what stops :data:`ALIASES` swallowing the whole query string.

	Every listing's flat parameters — `project`, `q`, `limit` — arrive here too, and each is
	owned by `api/query.refuse_unknown` rather than by this grammar. A version that treated any
	unrecognised flat name as a filter would refuse them all by name as unknown *fields*, which
	is a confident wrong answer about a parameter the route genuinely accepts.
	"""

	assert subroutine.domain.filtering.understood(
		[("project", "subroutine"), ("q", "colour"), ("limit", "5")], entity="task"
	) == []

	# And an alias belongs to the entity that declares it: a document has no deadline pair.
	assert subroutine.domain.filtering.understood(
		[("due_after", "2026-08-03")], entity="document"
	) == []


def test_a_flat_name_is_skipped_here_and_refused_where_nobody_else_owns_it () -> None:
	"""`SR#1626`. The division of labour, pinned — because the obvious tidy-up breaks HTTP.

	``understood`` **skips** a parameter with no separator, and that is correct on the surface
	it was written for: over HTTP ``status``, ``limit`` and ``project`` are real query
	parameters belonging to the endpoint, and ``api.query.refuse_unknown`` refuses the ones
	nobody declared. Making this function strict would refuse every listing that carries one.

	It is **wrong wherever no such neighbour exists**, which is the terminal's ``--filter`` and
	the agent surface's ``filter``: those namespaces are only ever filters, so a flat name is
	nobody's and was being dropped in silence. Both parsers call
	:func:`refuse_names_that_are_not_filters` first.

	**This is written as one test on purpose.** The two halves are a single decision about who
	owns what, and asserting them apart is how a later reader comes to believe the skip is a
	defect — which is what the sentence removed from ``understood``'s docstring encouraged. The
	comment that said *"nothing is quietly ignored"* was true of one caller and read as a claim
	about the program.
	"""

	# The mixed namespace: skipped, not refused, so an endpoint's own parameters survive.
	assert subroutine.domain.filtering.understood(
		[("status", "open"), ("created_at.gte", "yesterday")], entity="task"
	) == subroutine.domain.filtering.understood(
		[("created_at.gte", "yesterday")], entity="task"
	), "understood stopped skipping a flat name, which is what HTTP relies on"

	# The namespace that is only filters: refused, by name, with the shape.
	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.filtering.refuse_names_that_are_not_filters(
			{"status": "open", "created_at.gte": "yesterday"}
		)

	assert "'status'" in str(refused.value)

	# **An alias survives both**, and it is the case a rule about the separator alone breaks:
	# `due_before` carries no separator and is a filter.
	subroutine.domain.filtering.refuse_names_that_are_not_filters({"due_before": "today"})

	assert subroutine.domain.filtering.understood(
		[("due_before", "today")], entity="task"
	), "an alias stopped resolving"


#: Query parameters that mean the same thing on every listing and say nothing about an item's
#: own fields — `SR#2175`.
#:
#: **Keyed by name alone, and that is safe only because of the admission test below**, which
#: refuses an entry reaching one entity. A reason written while looking at one listing cannot be
#: trusted to hold on another — that is this item's own defect one level up, and a global excuse
#: is exactly how it would be reintroduced. A parameter that exists on a single listing is
#: entity-specific whatever it is called, so it belongs in :data:`NOT_A_PROPERTY` where its
#: reason sits beside the listing it was written about.
EVERY_LISTING: dict[str, str] = {
	# §14.10, and `api/shaping.py` takes already-rendered views for exactly this reason: a
	# display parameter with a path into the WHERE clause would be a scoping bug wearing a
	# formatting hat. A test already asserts the row set is identical across all three formats.
	"fields": "which fields to render, never which rows there are",
	"format": "how densely to render them, never which rows there are",
	# Paging is one question answered a page at a time. It reaches the query and does not change
	# what is being asked, which is the distinction this register turns on.
	"limit": "how much of the answer to return at once",
	"cursor": "where the previous page stopped — `domain.paging`",
	"include_total": "§8.4's opt-in count, which is a second query about the same question",
	# The other two thirds of `#1803`'s one declaration. Both are derived from the same
	# `Property` this guard compares against, so a field orderable or groupable without being
	# filterable is already argued in the registry rather than here.
	"order": "which end to read from — `Property.orderable` is the registry's half of it",
	"group_by": "which axis to arrange the page on — `Property.groupable` is its half",
	# `#1285`: a cap is a display choice and never a membership rule, and only the last bucket
	# may cap in the query because nothing follows it.
	"group_limit": "how many to show per group, which is a display choice and not a membership "
	"rule",
	"include": "§8.5's embedding — what to add to each row, not which rows to have",
	# **Which world, not which items in it.** Resolved before `domain/scoping.py` narrows
	# anything, and its meaning is identical on all five listings — which is what earns it a
	# place in a register keyed by name.
	"workspace_id": "which workspace the listing reads, resolved before any scoping",
}

#: Parameters that do narrow a listing's rows and are not properties in its registry —
#: `SR#2175`.
#:
#: **Keyed by entity and by name, because every reason here is about one listing.** Several
#: names appear twice and that repetition is the finding rather than noise: `SR#1829` is written
#: as *four flat parameters* and they are a task's four, while `status`, `type` and `project`
#: are flat on documents as well. A register keyed by name would have hidden exactly that.
#:
#: **Deleting an entry is what closes the item it names**, like every other allow-list here.
NOT_A_PROPERTY: dict[tuple[str, str], str] = {
	# **`status` and `type` have gone from here, which is what closed that half of `SR#1829`.**
	# Both are `REFERENCE` properties on both entities now, resolved through the same
	# `status_for` and `item_type_for` the flat spelling uses — so an unknown key is refused by
	# name with the workspace's own vocabulary listed, rather than answered with an empty page.
	#
	# **`project` has gone too**, and it is what widened `Where`: a project is resolved against
	# what this *caller* may see, so the predicate needs a principal and the workspace object
	# rather than the ids a subquery narrows by.
	# **`subtree` is the last flat narrowing on a task, and it is now an older spelling rather
	# than a gap** — `SR#2180`, Simon's decision of 2026-09-07. The second question `parent`
	# carried is `under`, a field of its own, so the registry answers both; this stays because
	# it is shipped and published, exactly as `due_before` and `due_after` do.
	#
	# **It cannot become a `Property` and that is why it needed a decision**: a boolean that
	# modifies another field is not a field, so leaving it as the only way to ask would have put
	# the question outside the grammar for good.
	("task", "subtree"): "an older spelling of `under`, kept because it is published — SR#2180",
	# A project is *reached* by its address rather than narrowed to by its name, which is the
	# reason `PROJECT_PROPERTIES` already gives for `key`, `title` and `path` being orderable
	# and unaskable. `SR#1804` is where a REFERENCE kind would change that; nobody has asked.
	("project", "parent"): "a project is reached by its address, not narrowed to by its name",
	# **Search, not a field** — `SR#1806` is the line that gives it a spelling, and `SR#1801` §8
	# is why `title:foo` is a filter wearing search syntax rather than the other way round.
	("task", "q"): "words to look for, which is search rather than a comparison — SR#1806",
	("document", "q"): "the same, and `q` already matches a document's title",
	# **Computed from other rows**, so there is no column to compare. `#69` made readiness a
	# filter by design for that reason: blockers and dates across the graph rather than a field.
	("task", "ready"): "computed across blockers and dates, so it is a predicate over the graph "
	"rather than a column",
	# **The same shape as `ready` and settled the same way** — `SR#1600`, Simon 2026-09-07.
	# *Assigned to me, or to nobody, or held by me* is three columns ORed against the caller,
	# and it takes no value: a `Property` needs a column and a kind, and a boolean is neither.
	# So it is a flat parameter by decision rather than by omission, exactly as `ready` is.
	("task", "to_act_on"): "three columns ORed against the caller, taking no value at all — a "
	"boolean is not a field, and `ready` is the precedent",
	# A band over `snoozed_until` against now, which is `SR#1805`'s distinction: the column is
	# filterable and orderable, and *startable against put off* is a fact about an instant that
	# a comparison cannot state.
	("task", "deferred"): "a band over `snoozed_until` against now, not a comparison with it",
	# **Defaults about the absence of a filter.** Each decides what an unnarrowed listing
	# means, so neither has a value to compare; a property would have to be *the default*,
	# which is not a field.
	("task", "include_completed"): "what an unnarrowed listing means, not a narrowing of one",
	("project", "include_archived"): "the same shape, on the other listing",
	# Soft-deleted rows are outside the readable set to begin with, so this *widens* the scope
	# rather than narrowing it — the opposite direction from everything a registry entry does.
	("task", "deleted"): "widens the readable set to include soft-deleted rows",
	("document", "deleted"): "the same, on the other entity",
	# **§5.11's resumable cursor, and the registry's own head says why it is not a filter**:
	# `since` is inclusive-with-dedupe, where a comparison would be an ordinary one, and two
	# spellings of one number where one quietly loses the resume guarantee is `SR#1017`'s shape.
	("event", "since"): "§5.11's resumable cursor, which is stronger than a comparison",
	("event", "before"): "the other end of that cursor",
	# The feed always runs forwards and the caller picks which end to start from, which is why
	# `EVENT_PROPERTIES` offers no ordering at all rather than one contradicting the cursor.
	("event", "newest"): "which end of the feed to start from — the ordering this listing has "
	"instead of `order`",
	("event", "oldest"): "the same, on the journal",
	# **`actor` has gone from here** — `SR#2178`, which is the one entry this register ever held
	# that was a gap rather than an argument. `me` meaning *this credential* rather than this
	# account turned out to be carryable after all: the group compiles both readings, so the
	# flat and dotted spellings cannot answer about different rows.
	# Two values today, so it reads as an axis; nothing has asked to group by it and a
	# REFERENCE over a two-word vocabulary is `SR#1804`'s question rather than this one's.
	("project", "visibility"): "public or private, which is an axis nobody has asked to group "
	"or compare on",
}


def _listings () -> list[tuple[typing.Any, str, frozenset[str]]]:
	"""Return every route that declares a filter reader, with its entity and its flat names.

	**Derived from the mounted routers, which is the whole of `SR#2175`.** The population this
	guard checks is read off the application rather than listed here, so a listing that gains a
	parameter tomorrow is measured without anybody remembering to add it — which is `SR#405`'s
	rule, and the absence of it is why `tag` was flat on documents and dotted on tasks for as
	long as the registry existed.

	Takes no argument and reads ``ROUTERS`` because that is what the application mounts;
	``app.routes`` is full of opaque ``_IncludedRouter`` objects with no path at all.
	"""

	found = []

	for _prefix, router in subroutine.api.app.ROUTERS:
		for route in router.routes:
			reader = subroutine.api.filters.declared_by(route)

			if reader is None:
				continue

			declared = getattr(getattr(route, "dependant", None), "query_params", [])
			names = frozenset(
				alias for field in declared if (alias := getattr(field, "alias", None))
			)

			found.append((route, reader.entity, names))

	return found


def test_a_listing_can_be_asked_flatly_for_nothing_it_has_not_declared () -> None:
	"""**The direction nothing looked** — `SR#2175`, reported by an agent gathering documents.

	``?tag=instrument-source`` answered `200` on ``/v1/documents`` while ``tag.eq=…`` answered
	`422` naming the field, so one word meant two things on one endpoint depending on how it
	was spelled. The guard above drives every *published* filter and passed throughout, quite
	correctly: `tag` was not published for documents, and a forward check cannot see a
	capability that was never declared.

	So this asks the other question. Every flat parameter a listing accepts is one of three
	things, and the first two are already written down somewhere better than a list:

	1. a :class:`Property` in that entity's registry — filterable or not, since a property that
       cannot be filtered on already carries its own ``because``;
	2. an older spelling of a dotted filter, from ``filtering.ALIASES``;
	3. an entry in :data:`EVERY_LISTING` or :data:`NOT_A_PROPERTY`, with a reason.

	A parameter that is none of them is a question the route answers and the grammar cannot ask
	— which is the defect `SR#2174` exists to close, one layer below the browser.
	"""

	listings = _listings()

	assert len(listings) >= 5, (
		f"only {len(listings)} listings declare a filter reader, and five were mounted when "
		f"this was written — a walk that reads nothing makes every entry above look stale"
	)

	unclassified = {}

	for route, entity, names in listings:
		registry = subroutine.domain.filtering.PROPERTIES.get(entity, {})
		aliases = subroutine.domain.filtering.ALIASES.get(entity, {})

		for name in sorted(names):
			if name in registry or name in aliases or name in EVERY_LISTING:
				continue

			if (entity, name) in NOT_A_PROPERTY:
				continue

			unclassified[(entity, name)] = route.path

	assert not unclassified, (
		"a listing accepts flat parameters that its registry has never heard of, and nothing "
		"says whether that is a decision: "
		+ ", ".join(f"{name!r} on {path} ({entity})" for (entity, name), path in sorted(unclassified.items()))
		+ ". Declare each as a Property, or say in EVERY_LISTING or NOT_A_PROPERTY why not."
	)


def test_no_excuse_here_names_a_parameter_that_has_gone () -> None:
	"""What makes an entry go away, asked of both registers — `SR#405`'s rule.

	An allow-list with a written reason has to fail when the reason expires as well as when a
	new case appears; otherwise an entry outlives the thing it excused and goes on reading as a
	considered decision. Three entries in ``test_reach`` did exactly that, all at once, and
	stayed invisible because every other check passed.

	**This is the half that closes `SR#1829`.** Converting `status`, `type`, `project` and
	`parent` to registry entries makes their entries above stale, so deleting them is part of
	that work rather than something to remember afterwards.
	"""

	accepted: dict[str, set[str]] = {}

	for _route, entity, names in _listings():
		accepted.setdefault(entity, set()).update(names)

	everywhere = set().union(*accepted.values()) if accepted else set()

	stale = sorted(name for name in EVERY_LISTING if name not in everywhere)

	assert not stale, f"EVERY_LISTING excuses parameters no listing declares: {stale}"

	gone = sorted(
		f"{entity}.{name}"
		for entity, name in NOT_A_PROPERTY
		if name not in accepted.get(entity, set())
	)

	assert not gone, f"NOT_A_PROPERTY excuses parameters no listing declares: {gone}"

	settled = sorted(
		f"{entity}.{name}"
		for entity, name in NOT_A_PROPERTY
		if name in subroutine.domain.filtering.PROPERTIES.get(entity, {})
	)

	assert not settled, (
		f"NOT_A_PROPERTY still excuses what the registry now declares: {settled} — the entry "
		f"is what the conversion had left to delete"
	)


def test_a_reason_written_once_may_not_cover_a_listing_it_never_saw () -> None:
	"""**Why :data:`EVERY_LISTING` may be keyed by name at all** — `SR#2175`.

	A register keyed by name applies its reason to every entity, including ones nobody had in
	front of them when they wrote it. That is safe for *paging and rendering*, whose meaning
	cannot vary by entity, and unsafe for anything else: excusing `parent` globally on the
	grounds that a project is reached by its address would silence a task's `parent`, which
	`SR#1829` says is the opposite of settled.

	Reaching two entities is the cheapest mechanical stand-in for *this reason is not about one
	listing*, and it is what makes the split a rule rather than a judgement. A parameter on one
	listing belongs in :data:`NOT_A_PROPERTY`, where its reason sits beside it.
	"""

	entities: dict[str, set[str]] = {}

	for _route, entity, names in _listings():
		for name in names:
			entities.setdefault(name, set()).add(entity)

	parochial = sorted(
		f"{name} (only {sorted(entities.get(name, set()))})"
		for name in EVERY_LISTING
		if len(entities.get(name, set())) < 2
	)

	assert not parochial, (
		f"EVERY_LISTING carries a reason written about one entity: {parochial} — move it to "
		f"NOT_A_PROPERTY, keyed by the listing it is about"
	)


def test_naming_a_finished_status_reaches_finished_work_however_it_is_spelled (
	world: World,
) -> None:
	"""**The behaviour `SR#1829` said had to survive the conversion** — `SR#1032`'s rule.

	``?status=done`` names a *key*, and `SR#1032` made that reach finished work as
	unambiguously as naming the category does: ``subroutine list --status done`` answered
	nothing on an instance holding five items finished that fortnight, because a listing hides
	finished work unless asked and the flat parameter did not count as asking.

	The dotted spelling is the same request. A `REFERENCE` entry that compiled to
	``status_id == x`` and nothing else would answer `[]` for every finished status on every
	listing — a plausible, complete, wrong answer, and a quiet regression on a listing's
	commonest narrowing.

	**Both directions matter.** Naming an unfinished status must not start dragging finished
	work in, which is what a fix that simply widened whenever `status` was mentioned would do.
	"""

	assert world.call("POST", "/v1/tasks/1/complete").status_code == 200

	assert world.titles("/v1/tasks?status=done") == ["the 1st"], "the flat spelling regressed"
	assert world.titles("/v1/tasks?status.eq=done") == ["the 1st"]

	# **`in` counts if any of the named statuses is a finished one**, because the caller is
	# asking for those rows and one of them is unreachable otherwise.
	assert "the 1st" in world.titles("/v1/tasks?status.in=open,done")

	# And naming only unfinished statuses reaches no finished work, as before.
	assert "the 1st" not in world.titles("/v1/tasks?status.eq=open")
	assert "the 1st" not in world.titles("/v1/tasks?status.in=open,blocked")


def test_excluding_completion_beside_a_mixed_status_filter_is_a_question_not_a_contradiction (
	world: World,
) -> None:
	"""**A case that could not be written until ``in`` existed** — `SR#1829`.

	``status=done&include_completed=false`` is refused, and rightly: *work whose status is done,
	and no finished work* admits nothing. ``status.in=open,done`` with the same exclusion is a
	different sentence — it is *the open ones*, which is coherent and non-empty.

	**So the two questions ``completion_wanted`` used to answer with one variable have parted
	company.** Whether the listing should *reach* finished work is **any** named status being
	finished, because the caller asked for those rows; whether the request *admits nothing* is
	**all** of them being finished. Every request writable before this item collapses the two,
	which is why they shared a name.
	"""

	assert world.call("POST", "/v1/tasks/1/complete").status_code == 200

	# The unchanged case: one finished status, and nothing is left.
	refused = world.call("GET", "/v1/tasks?status.eq=done&include_completed=false")

	assert refused.status_code == 422, refused.text
	assert "status='done'" in refused.json()["errors"][0]["message"]

	# The new one: some of them are unfinished, so the answer is those.
	answered = world.titles("/v1/tasks?status.in=open,done&include_completed=false")

	assert "the 1st" not in answered, "completion was excluded and finished work came back"
	assert answered == world.titles("/v1/tasks?status.eq=open")


def test_a_named_project_means_that_area_of_work_however_it_is_spelled (
	world: World,
) -> None:
	"""**`SR#320`'s rule, carried onto the dotted spelling** — `SR#1829`.

	A named project means *that area of work*, not that one node: every listing that took a
	``project`` once compared ``project_id`` to a single id, so a parent's listing excluded its
	own children and a hierarchy whose parent answered for none of its contents was a
	decoration. The registry entry compiles through ``scoping.within_project``, the same
	predicate the flat parameter uses, so the two cannot part company.

	**And a project the caller cannot see is *not found* rather than an empty page** — §7.3a's
	distinction, and the reason this filter needs a principal rather than a username. An
	unresolved key answered with `[]` would be a plausible, complete, wrong answer.
	"""

	parent = world.call("POST", "/v1/projects", json={"key": "area", "title": "An area"})

	assert parent.status_code == 201, parent.text

	child = world.call(
		"POST",
		"/v1/projects",
		json={"key": "under", "title": "Underneath", "parent": "area"},
	)

	assert child.status_code == 201, child.text

	made = world.call("POST", "/v1/tasks", json={"title": "filed below", "project": "under"})

	assert made.status_code == 201, made.text

	assert world.titles("/v1/tasks?project=area") == ["filed below"], "the flat spelling regressed"
	assert world.titles("/v1/tasks?project.eq=area") == ["filed below"]

	# **`in` is *any of these areas*, ORed** — so it reaches both subtrees and neither alone
	# would answer with what the other holds.
	assert world.titles("/v1/tasks?project.in=under,inbox") == sorted(
		world.titles("/v1/tasks?project.eq=under") + world.titles("/v1/tasks?project.eq=inbox")
	)
	assert "filed below" in world.titles("/v1/tasks?project.in=under,inbox")
	assert "filed below" not in world.titles("/v1/tasks?project.eq=inbox")

	# **A key nobody has is refused by name**, which is what routing through `selection.project`
	# buys and what a bare id comparison could not do.
	refused = world.call("GET", "/v1/tasks?project.eq=nosuchproject")

	assert refused.status_code == 404, refused.text


def test_a_written_line_narrows_and_searches_in_one_parameter (world: World) -> None:
	"""**The grammar over the wire** — `SR#1806`, design `SR#1801` §6.

	``q`` is the parameter a search box has always sent, and the line is parsed out of it: terms
	naming a registry field become comparisons, and what is left is searched for. So a caller
	writes one thing and the server does both, which is what *sugar over the registry* means —
	by the time the endpoint sees a comparison it cannot tell which spelling it arrived in.
	"""

	made = world.call(
		"POST", "/v1/tasks", json={"title": "deploy script", "type": "bug", "urgency": 4}
	)

	assert made.status_code == 201, made.text

	other = world.call(
		"POST", "/v1/tasks", json={"title": "deploy script", "type": "chore", "urgency": 4}
	)

	assert other.status_code == 201, other.text

	# The term narrows and the words search, from one string.
	found = world.call("GET", "/v1/tasks?q=type:bug deploy").json()["items"]

	assert [item["title"] for item in found] == ["deploy script"]
	assert found[0]["type"] == "bug", "the term did not narrow"

	# Without the term, both are found — so the narrowing above was real.
	assert len(world.call("GET", "/v1/tasks?q=deploy").json()["items"]) == 2


def test_a_search_that_means_nothing_to_the_grammar_behaves_exactly_as_before (
	world: World,
) -> None:
	"""**The property that makes reusing ``q`` safe** — `SR#1806`.

	Reinterpreting a shipped parameter is how a caller comes to believe it asked something it
	did not, and this one is published, documented and in every client. It is safe because only
	a term naming a field the registry *really carries* is taken out of the text: a colon in an
	ordinary query is ordinary text, so every search written before this grammar existed asks
	the same question it always did.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "stand-up at 15:30 on Monday"})

	assert made.status_code == 201, made.text

	found = world.call("GET", "/v1/tasks?q=15:30").json()

	assert [item["title"] for item in found["items"]] == ["stand-up at 15:30 on Monday"]
	assert found["page"]["unread"] is None, "words are not a term that failed"


def test_a_term_that_cannot_be_read_is_searched_for_and_reported (world: World) -> None:
	"""`SR#615`'s rule, over the wire and in the envelope — `SR#1806`.

	``created_at:today`` names a real field with an operator it deliberately refuses: two
	instants are equal to the microsecond and almost never to the caller (`SR#815`). Dropping
	the term would be a plausible, complete, wrong answer and refusing the request would be
	wrong about a line that *was* answered — so it is searched for as text and said.

	**And a listing has nowhere else to say it**, which is why ``page.unread`` exists at all.
	"""

	answer = world.call("GET", "/v1/tasks?q=created_at:today").json()
	reported = answer["page"]["unread"]

	assert reported is not None and len(reported) == 1, answer["page"]
	# The report names the term and the operators the field really takes.
	assert "created_at:today" in reported[0]

	for operator in ("gt", "gte", "lt", "lte"):
		assert operator in reported[0], reported[0]


def test_a_listing_can_be_narrowed_to_what_somebody_created (world: World) -> None:
	"""`SR#1577` — Simon's question (a) of 2026-08-29, *items created by me*.

	**Missing on every surface at once until now.** `created_by` is reported on every row and
	in `selectable`, and no listing accepted it — the `test_api_writability` family with the
	direction reversed, where a caller reads a field on every row and cannot ask for the rows
	carrying it.

	**`touched_by` is the nearest thing that worked and answers a different question.** It
	reads the event feed, so it means *worked on* rather than *created*, and on this instance
	it returned the same items for two accounts because both had touched them.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "filed by the caller"})

	assert made.status_code == 201, made.text

	# **`me` is the calling account**, resolved exactly as `SR#518` made it for `assignee` — or
	# one word means two things across two surfaces.
	assert "filed by the caller" in world.titles("/v1/tasks?created_by.eq=me")
	assert "filed by the caller" in world.titles(
		f"/v1/tasks?created_by.eq={world.user.username}"
	)

	# **And through the written line**, because the grammar compiles to the registry — one
	# entry gave three spellings and nothing was written twice (`SR#1806`).
	assert "filed by the caller" in world.titles("/v1/tasks?q=created_by:me")

	# **`is` means something real here because the column is nullable** — a row the system
	# wrote during setup, before any user existed to attribute it to. Made explicitly rather
	# than assumed: the fixture's own tasks *are* attributed, which the first version of this
	# case discovered by asserting that it had found something.
	orphan = world.call("POST", "/v1/tasks", json={"title": "written during setup"})

	assert orphan.status_code == 201, orphan.text

	# **The id comes back as text and the column is a UUID**, so it is converted rather than
	# passed through — `session.get` on the raw string raises inside the driver rather than
	# answering *not found*.
	row = world.session.get(
		subroutine.db.models.work.Task, uuid.UUID(orphan.json()["id"])
	)

	assert row is not None

	row.created_by = None
	world.session.flush()

	unattributed = world.titles("/v1/tasks?created_by.is=unset")

	assert unattributed == ["written during setup"]
	assert "filed by the caller" in world.titles("/v1/tasks?created_by.is=set")


def test_a_document_can_be_narrowed_to_what_somebody_created (world: World) -> None:
	"""The same question on the other entity — `SR#1577`.

	`GET /v1/documents` did not accept `created_by` either, so this was one gap and not two.
	"""

	made = world.call("POST", "/v1/documents", json={"title": "written by the caller"})

	assert made.status_code == 201, made.text
	assert world.titles("/v1/documents?created_by.eq=me") == ["written by the caller"]


def test_parent_is_one_level_and_under_is_the_whole_tree (world: World) -> None:
	"""**`SR#1829`'s fourth, decided on `SR#2180`** — Simon, 2026-09-07: two fields, and the
	second one is `under`.

	``?parent=7`` is *directly under #7* and ``?parent=7&subtree=true`` is *anywhere below it*:
	one parameter answering two questions, which a `Property` cannot express because a boolean
	modifying another field is not a field. Keeping `subtree` would have left the second
	question outside the registry — the search line could say *direct children* and could not
	say *everything below*, which is `SR#2174`'s own complaint recreated inside the work meant
	to fix it.

	**`under` excludes the item itself**, which is what the flat spelling has always done: a
	reader asking what a milestone contains does not want the milestone in the answer.
	"""

	# **Built with `move` rather than a create field**, because a task is filed at the top and
	# put under something afterwards — "no parent" and "unchanged" have to be distinguishable,
	# which is why that is a body rather than a query parameter (§8.3).
	made = {}

	for title in ("the parent", "the child", "the grandchild"):
		answer = world.call("POST", "/v1/tasks", json={"title": title})

		assert answer.status_code == 201, answer.text

		made[title] = answer.json()

	for title, above in (("the child", "the parent"), ("the grandchild", "the child")):
		moved = world.call(
			"POST",
			f"/v1/tasks/{made[title]['ref']}/move",
			json={"parent": str(made[above]["ref"])},
		)

		assert moved.status_code == 200, moved.text

	parent = made["the parent"]

	one = f"/v1/tasks?parent.eq={parent['ref']}"
	all_of_it = f"/v1/tasks?under.eq={parent['ref']}"

	assert world.titles(one) == ["the child"]
	assert world.titles(all_of_it) == ["the child", "the grandchild"]

	# **The two really are different questions**, or the pair is one field under two names.
	assert world.titles(one) != world.titles(all_of_it)

	# **And the older flat spelling still answers both**, documented rather than removed —
	# `due_before` and `due_after` are the precedent.
	assert world.titles(f"/v1/tasks?parent={parent['ref']}") == world.titles(one)
	assert world.titles(
		f"/v1/tasks?parent={parent['ref']}&subtree=true"
	) == world.titles(all_of_it)

	# **Through the written line too**, because the grammar compiles to the registry — which is
	# the whole reason this was two fields rather than a modifier (`SR#1806`).
	assert world.titles(f"/v1/tasks?q=under:{parent['ref']}") == world.titles(all_of_it)


def test_under_refuses_the_question_parent_already_answers (world: World) -> None:
	"""**One question may not have two spellings** — `SR#1829`, and it nearly did.

	``under``'s predicate walks ``path`` and never compares a column, so the column on its
	declaration is only there for ``_allowed`` to read. Written with ``parent_task_id`` — the
	obvious choice — it made ``under.is=unset`` legal, and that compiles to *has no parent*,
	which is exactly ``parent.is=unset``: two spellings of one question, on a pair of fields
	added in the same commit.

	**The column is the item's own identity instead**, which is the idiom ``tag`` established
	for the same reason: ``Task.id`` is ``NOT NULL``, so ``_allowed`` refuses ``is`` without
	anybody writing a rule.
	"""

	assert "is" in subroutine.domain.filtering.filters("task")["parent"].operators
	assert "is" not in subroutine.domain.filtering.filters("task")["under"].operators

	refused = world.call("GET", "/v1/tasks?under.is=unset")

	assert refused.status_code == 422, refused.text
	assert "under" in refused.text

	# **The question it would have answered is still askable, by its one spelling.**
	assert world.call("GET", "/v1/tasks?parent.is=unset").status_code == 200


def test_a_listing_can_be_narrowed_to_what_is_yours_to_act_on (world: World) -> None:
	"""`SR#1600`, and the predicate was built, correct, and reachable from one surface.

	``readiness.yours_to_act_on`` — *assigned to me, **or to nobody**, or held by me* — has
	existed since `SR#1265` and applied only in the agenda. `SR#1265` said so deliberately:
	*"No other view is narrowed by assignee."* That was right while a listing was one person's
	backlog and stopped being right the moment a second principal picked work off it.

	**Driven on the live instance 2026-08-29**: ``list --ready`` returned 219 rows — 195
	unassigned, 21 one person's, 3 an agent's. So an agent asking *what can I start* either
	took work belonging to somebody or ignored 195 items belonging to nobody, and there was no
	third question it could ask.

	**Named ``to_act_on`` and not ``mine``** (Simon, 2026-09-07). ``assignee=me`` already means
	*strictly assigned*, and a familiar word that reads narrowly fails silently — by 195 rows.
	"""

	# **The username rather than `me`**: the sentinel is opt-in per call site (`SR#518`), and
	# a create is deliberately not one of the sites that takes it.
	mine = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "given to me", "assignee": str(world.user.username)},
	)

	assert mine.status_code == 201, mine.text

	nobody = world.call("POST", "/v1/tasks", json={"title": "given to nobody"})

	assert nobody.status_code == 201, nobody.text

	found = world.titles("/v1/tasks?to_act_on=true")

	assert "given to me" in found
	assert "given to nobody" in found, (
		"the unassigned pool is the whole reason this is not `assignee=me` — it was 195 of 219 "
		"ready rows on the live instance"
	)

	# **And it really is wider**, or the two are one question under two names.
	assert world.titles("/v1/tasks?assignee=me") == ["given to me"]
	assert set(world.titles("/v1/tasks?assignee=me")) < set(found)

	# **It composes with `ready` rather than replacing it** — *what can I start* and *whose is
	# it* are separate questions, and the caller asking both is the one this was filed for.
	assert world.call("GET", "/v1/tasks?ready=true&to_act_on=true").status_code == 200


def test_work_given_to_somebody_else_is_not_yours_to_act_on (world: World) -> None:
	"""The half that makes the filter worth having — `SR#1600`.

	A predicate that returned everything would pass the case above and be useless. What
	``to_act_on`` must exclude is exactly what a person or an agent must **not** quietly pick
	up: work carrying somebody else's name.
	"""

	# **A member of this workspace, not merely an account.** Assigning work resolves a name
	# *within* the workspace, so an instance-wide account that is not a member is refused — the
	# first version of this case created one through the route and was turned down by name.
	colleague = _an_agent_of(world, "colleague")

	theirs = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "given to them", "assignee": str(colleague.username)},
	)

	assert theirs.status_code == 201, theirs.text

	found = world.titles("/v1/tasks?to_act_on=true")

	assert "given to them" not in found, (
		f"work assigned to somebody else came back as yours to act on: {found}"
	)
	assert "given to them" in world.titles(
		f"/v1/tasks?assignee={colleague.username}"
	), "the fixture did not assign it, so the exclusion above proved nothing"


def test_a_document_can_be_filed_under_another_and_a_listing_can_ask (world: World) -> None:
	"""**`SR#2173`, Simon 2026-09-07** — one document per instrument, all at top level, all on
	the board, and nothing to collapse them behind.

	**Most of this was built and unreachable**, which is `SR#143`'s pattern: `Document.parent_id`
	has been a column, indexed and reported, since `SR#1534`, and
	``POST /v1/documents/{ref}/move`` has set it since `SR#294`. **No listing could read it
	back**, so a document could be nested and then never found that way again.

	**A parent is just a document** — `SR#84`'s rule for tasks, applied unchanged. No folder
	type and no second tree; and it is called a *parent*, because a second vocabulary for one
	relation is what `SR#1547` refuses.
	"""

	above = world.call("POST", "/v1/documents", json={"title": "Instrument specs"}).json()
	inside = world.call("POST", "/v1/documents", json={"title": "One instrument"}).json()
	deeper = world.call("POST", "/v1/documents", json={"title": "A section of it"}).json()

	for row, onto in ((inside, above), (deeper, inside)):
		moved = world.call(
			"POST", f"/v1/documents/{row['ref']}/move", json={"parent": str(onto["ref"])}
		)

		assert moved.status_code == 200, moved.text

	assert world.titles(f"/v1/documents?parent.eq={above['ref']}") == ["One instrument"]
	assert world.titles(f"/v1/documents?under.eq={above['ref']}") == [
		"A section of it", "One instrument",
	]

	# **The question the board needs: what is at the top** — which is what a cluttered board of
	# instrument specs was missing, and it is `is` coming free on a nullable column.
	top = world.titles("/v1/documents?parent.is=unset")

	assert "Instrument specs" in top
	assert "One instrument" not in top

	# **And through the written line**, because the grammar compiles to the registry.
	assert world.titles(f"/v1/documents?q=under:{above['ref']}") == world.titles(
		f"/v1/documents?under.eq={above['ref']}"
	)


def test_asking_a_document_listing_about_a_tasks_ref_is_refused_by_name (
	world: World,
) -> None:
	"""**One counter numbers both kinds** (§6.2), so a ref names exactly one of them — `SR#2173`.

	`#1` is a task in this fixture, and a document's tree filter resolving it would either
	answer emptily or narrow by a parent that is not a document. The resolver refuses it and
	says which kind it really is, which is `SR#488`'s rule: *"there is no document 1"* about
	something the caller has just listed is a refusal naming a cause it has not established.
	"""

	refused = world.call("GET", "/v1/documents?parent.eq=1")

	assert refused.status_code == 404, refused.text
	assert "task" in refused.text.lower(), refused.text


def test_a_document_says_how_many_are_filed_under_it (world: World) -> None:
	"""`SR#2173`'s fourth part — `SR#84`'s `3/3` beside a milestone, on the other kind.

	**Derived on every read** (design `SR#1801` §7). A stored counter is a second copy of a
	fact and drifts through any door the write path does not own — an import, a restore, a
	hand-run UPDATE — silently, because nothing compares them.

	**Direct contents rather than the whole subtree**, because that is what a reader asking
	what a specification holds means. The subtree already has a spelling and answers the other
	question exactly.
	"""

	above = world.call("POST", "/v1/documents", json={"title": "Instrument specs"}).json()
	inside = world.call("POST", "/v1/documents", json={"title": "One instrument"}).json()
	deeper = world.call("POST", "/v1/documents", json={"title": "A section"}).json()

	for row, onto in ((inside, above), (deeper, inside)):
		assert world.call(
			"POST", f"/v1/documents/{row['ref']}/move", json={"parent": str(onto["ref"])}
		).status_code == 200

	counted = {
		one["ref"]: one["sub_documents"]
		for one in world.call(
			"GET", "/v1/documents?fields=ref,sub_documents"
		).json()["items"]
	}

	assert counted[above["ref"]] == 1, "the grandchild was counted as contents"
	assert counted[inside["ref"]] == 1
	assert counted[deeper["ref"]] == 0

	# **A deleted child is not counted**, because the number sits beside a title and says what
	# is there — a count including the trash sends a reader looking for rows no listing shows.
	assert world.call("DELETE", f"/v1/documents/{deeper['ref']}").status_code in (200, 204)

	after = {
		one["ref"]: one["sub_documents"]
		for one in world.call(
			"GET", "/v1/documents?fields=ref,sub_documents"
		).json()["items"]
	}

	assert after[inside["ref"]] == 0


def test_a_document_names_the_one_it_is_filed_under (world: World) -> None:
	"""`SR#2201`. A `parent_id` alone is not an address.

	A ref is how this product names an item (§6.2), so a view reporting the id and nothing
	else forces every client to fetch the parent before it can print a word — review dimension
	4's second call, multiplied by the page. `Task` has carried `parent_ref` and `parent_title`
	since `SR#510` for exactly that reason and a document carried neither, which nothing
	noticed because until `SR#2173` nothing could nest one.
	"""

	above = world.call("POST", "/v1/documents", json={"title": "Instrument specs"}).json()
	inside = world.call(
		"POST", "/v1/documents", json={"title": "One instrument", "parent": str(above["ref"])}
	)

	assert inside.status_code == 201, inside.text

	listed = {
		one["ref"]: one
		for one in world.call(
			"GET", "/v1/documents?fields=ref,title,parent_ref,parent_title"
		).json()["items"]
	}

	assert listed[inside.json()["ref"]]["parent_ref"] == above["ref"]
	assert listed[inside.json()["ref"]]["parent_title"] == "Instrument specs"

	# **Null together, and null honestly means top level** — not *this client may not see it*.
	assert listed[above["ref"]]["parent_ref"] is None
	assert listed[above["ref"]]["parent_title"] is None
