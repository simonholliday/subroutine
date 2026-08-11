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
import subroutine.db.models.identity
import subroutine.db.models.system
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.filtering
import subroutine.domain.instances

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
		session.execute(
			sqlalchemy.update(subroutine.db.models.work.Task)
			.where(subroutine.db.models.work.Task.id == uuid.UUID(created.json()["id"]))
			.values(created_at=when, updated_at=when)
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


def test_a_listing_that_declares_no_reader_still_refuses_a_dotted_name (
	world: World,
) -> None:
	"""**The other half of the seam, and the one that could fail silently.**

	`refuse_unknown` stops policing dotted names *only* where a route declares a reader. If it
	stopped policing them everywhere, a date filter sent to a listing that cannot honour one
	would be ignored and answered `200` — a complete, plausible, wrong answer, which is exactly
	the failure that module was written to prevent.
	"""

	answer = world.call("GET", "/v1/changes?created_at.gte=2026-08-01")

	assert answer.status_code == 422, answer.text
	assert "created_at.gte" in answer.text


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
		answer = world.call("GET", f"{published_path(published, entity)}?{name}=today")

		assert answer.status_code == 200, f"{name} is published and refused: {answer.text}"


def published_path (published: list[str], entity: str) -> str:
	"""Return the listing path for an entity, so the case above reads as one question."""

	return {"task": "/v1/tasks", "document": "/v1/documents", "project": "/v1/projects"}[
		entity
	]


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
