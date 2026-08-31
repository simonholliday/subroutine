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
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.filtering
import subroutine.domain.instances
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

	published = world.call("GET", "/v1/meta").json()["listings"][entity]["filters"]
	dotted = sorted(name for name in published if "." in name)

	assert dotted, f"{entity} publishes no dotted filters"
	assert dotted == sorted(subroutine.domain.filtering.names(entity))

	for name in dotted:
		# **The value follows the field's kind**, read from the registry rather than fixed —
		# `touched_by` takes a username, and driving every combination with `today` refused it
		# with *there is no account called 'today'*, which is the route working correctly.
		kind = subroutine.domain.filtering.FILTERS[entity][name.partition(".")[0]].kind
		value = _sample(kind, world)

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
}


def _sample (kind: subroutine.domain.filtering.Kind, world: World) -> str:
	"""Return something this kind of filter will accept."""

	if kind is subroutine.domain.filtering.WHO:
		return str(world.user.username)

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
