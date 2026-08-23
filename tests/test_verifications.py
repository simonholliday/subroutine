"""What was checked against a task, and which tree it was checked on — `#1121`.

docs/design.md §14.5's evidence half, and the sentence that governs every line of it comes
from `#593`:

    Self-reported evidence is a **record**, not a proof. An agent can post exit code 0
    without running anything.

So what these tests are about is the three properties that make a record worth keeping —
**durable, attributable, invalidatable** — and the third is the one with a design decision in
it. §14.5 measured staleness against `task.content_updated_at`, and that column does not move
when the *code* does; `#749` and `#893` are two releases this project published nothing from
for exactly that reason.
"""

import typing

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.work
import subroutine.domain.verifications
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP."""

	return test_api_tasks._world(session)


def _task (world: test_api_tasks.World, title: str = "The work") -> int:
	"""Make a task and return its ref."""

	response = world.call("POST", "/v1/tasks", json={"title": title})

	assert response.status_code == 201, response.text

	return int(response.json()["ref"])


def _record (
	world: test_api_tasks.World, ref: int, **body: typing.Any
) -> dict[str, typing.Any]:
	"""Record a check, defaulting it to one that passed."""

	body.setdefault("passed", True)
	response = world.call("POST", f"/v1/tasks/{ref}/verifications", json=body)

	assert response.status_code == 201, response.text

	return typing.cast(dict[str, typing.Any], response.json())


def _records (world: test_api_tasks.World, ref: int) -> list[dict[str, typing.Any]]:
	"""Read what has been checked against a task."""

	response = world.call("GET", f"/v1/tasks/{ref}/verifications")

	assert response.status_code == 200, response.text

	return typing.cast(list[dict[str, typing.Any]], response.json()["items"])


def test_a_record_carries_what_was_checked_and_what_it_was_checked_on (
	world: test_api_tasks.World,
) -> None:
	"""The whole feature in one case, and the tree is the half `#1121` is about."""

	ref = _task(world)
	written = _record(
		world, ref, summary="5,610 passed, 41 skipped", tree_hash="a" * 40, commit_sha="b" * 40
	)

	assert written["passed"] is True
	assert written["summary"] == "5,610 passed, 41 skipped"
	assert written["tree_hash"] == "a" * 40
	assert written["task_ref"] == ref
	assert written["recorded_by"] == world.user.username, "a record nobody is named on"

	assert [one["id"] for one in _records(world, ref)] == [written["id"]]


def test_a_record_without_a_tree_is_still_a_record (world: test_api_tasks.World) -> None:
	"""§1.4, and the guard Simon asked for before this was built.

	`NOT NULL` is the natural way to write `tree_hash` and would make a record impossible from
	a machine with no checkout — which is most of them, and which §1.4 forbids: no §14 entity
	may be *required* in order to do the ordinary thing.

	**It simply cannot expire**, and saying nothing is a different answer from saying it is
	current.
	"""

	ref = _task(world)
	written = _record(world, ref, summary="Checked by hand")

	assert written["tree_hash"] is None
	assert _records(world, ref) == [written]


def test_a_failing_check_is_kept (world: test_api_tasks.World) -> None:
	"""And is the more useful half of the pair.

	*This was tried and did not work* is what stops it being tried again, and a table holding
	only successes would be one nobody could learn from.
	"""

	ref = _task(world)
	written = _record(world, ref, passed=False, summary="3 failed in test_agenda")

	assert written["passed"] is False
	assert _records(world, ref)[0]["summary"] == "3 failed in test_agenda"


def test_records_are_newest_first (world: test_api_tasks.World) -> None:
	"""Unlike a comment thread, which is read from the beginning.

	A record is not a story: what a caller wants is the most recent thing that was checked,
	and everything older is context for it.
	"""

	ref = _task(world)
	first = _record(world, ref, summary="First")
	second = _record(world, ref, summary="Second")

	assert [one["id"] for one in _records(world, ref)] == [second["id"], first["id"]]


def test_staleness_is_the_readers_comparison_and_has_three_answers (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#1121`'s correction, and the reason `is_stale` is not a published field.

	§10.7 invariant 11 says it is derived and never stored, and it is — but the thing it is
	derived *from* is not on the row and is not on the instance either. Only somebody standing
	in the checkout can answer it.

	**Three answers, not two.** A record with no tree cannot expire and must not read as fresh,
	and a caller with no tree of its own cannot judge one that has. *Unknown* is honest and is
	different from *current*.
	"""

	world = test_api_tasks._world(session)
	ref = _task(world)
	world.call(
		"POST", f"/v1/tasks/{ref}/verifications", json={"passed": True, "tree_hash": "a" * 40}
	)
	bare = world.call("POST", f"/v1/tasks/{ref}/verifications", json={"passed": True})

	assert bare.status_code == 201

	rows = list(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.work.Verification).order_by(
				subroutine.db.models.work.Verification.ran_at
			)
		)
	)
	against_a_tree, against_nothing = rows

	assert subroutine.domain.verifications.is_stale(
		against_a_tree, tree_hash="a" * 40
	) is False
	assert subroutine.domain.verifications.is_stale(
		against_a_tree, tree_hash="c" * 40
	) is True
	assert subroutine.domain.verifications.is_stale(
		against_a_tree, tree_hash=None
	) is None, "a caller with no tree cannot judge one that has"
	assert subroutine.domain.verifications.is_stale(
		against_nothing, tree_hash="a" * 40
	) is None, "a record with no tree read as current"


@pytest.mark.parametrize(
	"value", ["not-a-hash", "a" * 65, "abc def", "deadbeef\\n; rm -rf /"]
)
def test_a_hash_that_is_not_one_is_refused (
	world: test_api_tasks.World, value: str
) -> None:
	"""Because it is compared for equality later, and a wrong one is silently permanent.

	A value with a stray newline or a `git rev-parse` error message in it would never match
	anything, so the record would read as permanently stale — a wrong answer that looks like a
	right one, which is worse than a refusal.
	"""

	ref = _task(world)
	refused = world.call(
		"POST", f"/v1/tasks/{ref}/verifications", json={"passed": True, "tree_hash": value}
	)

	assert refused.status_code == 422, refused.text
	assert "tree_hash" in refused.text


def test_an_empty_hash_means_none_was_given (world: test_api_tasks.World) -> None:
	"""Which is why blank is not in the refusals above.

	A shell interpolating `$(git rev-parse …)` outside a checkout produces an empty string, so
	*nothing was given* is the ordinary way this arrives — and refusing it would make the hook
	that produces these records fail on every machine without git.
	"""

	ref = _task(world)

	assert _record(world, ref, tree_hash="   ")["tree_hash"] is None


def test_a_hash_is_read_in_either_case (world: test_api_tasks.World) -> None:
	"""git prints lower case and a person may not, and equality does not forgive that."""

	ref = _task(world)
	written = _record(world, ref, tree_hash="ABCDEF1234567890" * 2)

	assert written["tree_hash"] == "abcdef1234567890" * 2


def test_recording_needs_the_right_to_change_the_task (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``task:write``, not ``task:read``.

	Posting evidence against somebody's work changes what that work says about itself, and a
	credential that may only look at a task should not be able to attach a claim to it.
	"""

	world = test_api_tasks._world(session)
	ref = _task(world)
	reader = test_api_tasks._world(session, scopes=["task:read"])

	# The same instance, a narrower credential. Its own workspace is a different one, so the
	# task is read through the first world's — which is what makes this about the scope.
	refused = world._replace(secret=reader.secret).call(
		"POST", f"/v1/tasks/{ref}/verifications", json={"passed": True}
	)

	assert refused.status_code in (403, 404), refused.text


def test_a_summary_longer_than_a_title_is_refused (world: test_api_tasks.World) -> None:
	"""It is one line somebody reads in a list, and a paragraph there is a body in the wrong field."""

	ref = _task(world)
	refused = world.call(
		"POST",
		f"/v1/tasks/{ref}/verifications",
		json={"passed": True, "summary": "x" * 600},
	)

	assert refused.status_code == 422, refused.text
	assert "summary" in refused.text


def test_a_record_reaches_the_tasks_history (world: test_api_tasks.World) -> None:
	"""Which is where somebody goes to find out what happened to something.

	The event names the record and carries the task as its subject — the pair `domain.comments`
	and `domain.links` already use — so it appears in that item's history without the history
	knowing what a verification is.
	"""

	ref = _task(world)
	_record(world, ref, summary="5,610 passed")
	history = world.call("GET", f"/v1/tasks/{ref}/events").json()["items"]

	assert any(one["entity_type"] == "verification" for one in history), history
