"""``POST /v1/recurrence/parse`` — the endpoint that makes a written repeat checkable.

`#94`, §6.7. **The property worth holding is that the answer comes back in different words
from the ones sent.** An endpoint that echoed the input would confirm nothing; this one reads
the *stored rule* back, so somebody who wrote "every other tuesday" can see it understood
*every other week, on Tuesday* and decide whether that is what they meant.

It writes nothing, so these are cheap and there is no cleanup.
"""

import datetime
import typing

import pytest
import sqlalchemy.orm

import subroutine.domain.recurrence
import test_api_tasks


@pytest.fixture()
def world (session: sqlalchemy.orm.Session) -> typing.Iterator[test_api_tasks.World]:
	"""An installation with one user, one workspace and an Inbox."""

	yield test_api_tasks._world(session)


def test_a_phrase_comes_back_as_a_rule_and_a_sentence (
	world: test_api_tasks.World,
) -> None:
	"""The whole endpoint: what it stores, what it means, and when it happens."""

	answer = world.call(
		"POST", "/v1/recurrence/parse", json={"text": "every other tuesday"}
	)

	assert answer.status_code == 200, answer.text

	body = answer.json()

	assert body["rule"] == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
	assert body["text"] == "every other tuesday"
	assert len(body["occurrences"]) == 5

	# **Different words from the ones sent**, which is the property that makes this a check
	# rather than a mirror. Asserted as a relation rather than as a literal, so it holds when
	# somebody improves the wording.
	assert body["description"] != body["text"]
	assert "Tuesday" in body["description"]


def test_a_rule_sent_directly_is_read_back_and_keeps_no_words (
	world: test_api_tasks.World,
) -> None:
	"""One field, two shapes — the reason ``due`` takes a date, a datetime and an expression."""

	answer = world.call(
		"POST", "/v1/recurrence/parse", json={"text": "FREQ=MONTHLY;BYMONTHDAY=30"}
	)

	assert answer.status_code == 200, answer.text

	body = answer.json()

	assert body["rule"] == "FREQ=MONTHLY;BYMONTHDAY=30"
	assert body["description"] == "every month, on the 30th"

	# Null rather than the rule repeated back: nobody wrote a sentence, and saying they did
	# would be inventing evidence about what a caller meant.
	assert body["text"] is None


def test_the_dates_are_computed_from_where_the_caller_says (
	world: test_api_tasks.World,
) -> None:
	"""A form asks *what would this mean*, and for an unfiled task the answer starts today.

	``from`` is what lets it ask about a task that starts somewhere else — and it is spelled
	``from`` on the wire because that is the English word, with the model aliasing it past
	Python's keyword rather than making every caller write ``from_``.
	"""

	answer = world.call(
		"POST",
		"/v1/recurrence/parse",
		json={
			"text": "every monday",
			"from": "2026-08-15T09:00:00Z",
			"timezone": "Europe/London",
		},
	)

	assert answer.status_code == 200, answer.text

	first = datetime.datetime.fromisoformat(answer.json()["occurrences"][0])

	assert first.astimezone(datetime.UTC).date() == datetime.date(2026, 8, 17)


def test_a_phrase_this_cannot_read_is_refused_exactly_as_a_create_would_refuse_it (
	world: test_api_tasks.World,
) -> None:
	"""**The same function, so the two answers cannot drift** — which is the whole reason to
	check first at all.

	A caller that previewed a phrase, was told it was fine, and then had the create refuse it
	would have been given a check worth less than nothing. Asserted by comparing the two
	refusals rather than by matching a string, so a reworded message keeps this honest.
	"""

	previewed = world.call(
		"POST", "/v1/recurrence/parse", json={"text": "every fortnight"}
	)

	assert previewed.status_code == 422

	created = world.call(
		"POST",
		"/v1/tasks",
		json={"text": "Water the plants", "due": "2026-09-01", "recurrence": "every fortnight"},
	)

	assert created.status_code == 422
	assert previewed.json()["detail"] == created.json()["detail"]
	assert previewed.json()["code"] == created.json()["code"]


def test_it_stores_nothing (world: test_api_tasks.World) -> None:
	"""A calculator. Nothing to undo if the answer was not what the caller wanted."""

	before = world.call("GET", "/v1/tasks").json()["items"]

	world.call("POST", "/v1/recurrence/parse", json={"text": "every day"})

	assert world.call("GET", "/v1/tasks").json()["items"] == before


def test_every_published_example_survives_the_round_trip (
	world: test_api_tasks.World,
) -> None:
	"""`#821`'s shape at the transport layer, rather than only in the domain.

	``/v1/meta`` will carry these, so they are what an agent learns the grammar from — and an
	example that parses in Python and 422s over HTTP would be the divergence this project keeps
	finding, on the surface with the least chance of anybody noticing.
	"""

	for text in subroutine.domain.recurrence.published()["examples"]:
		answer = world.call("POST", "/v1/recurrence/parse", json={"text": text})

		assert answer.status_code == 200, f"{text!r} answered {answer.status_code}"
		assert answer.json()["occurrences"], f"{text!r} named no dates"


def test_a_repeat_expands_into_the_dates_it_produces (
	world: test_api_tasks.World,
) -> None:
	"""`#94`, §6.7. **The endpoint that makes decision `#915` worth taking.**

	One occurrence is real and the rest are arithmetic, so *show me every birthday* is a
	question about a view rather than about the backlog — and a birthday stays one row for ever
	rather than becoming one row per year since 1974. Nothing is stored and nothing is
	materialised: a `GET` that wrote would break a read-only credential and race two readers.
	"""

	made = world.call(
		"POST",
		"/v1/tasks",
		json={"text": "Water the plants", "due": "2026-09-01", "recurrence": "every 3 days"},
	)

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	answer = world.call("GET", f"/v1/tasks/{ref}/occurrences?limit=4")

	assert answer.status_code == 200, answer.text

	body = answer.json()

	assert body["rule"] == "FREQ=DAILY;INTERVAL=3"
	assert body["description"] == "every 3 days"
	assert len(body["occurrences"]) == 4

	# **A rule with no end never runs out**, so *there are no more* and *I stopped counting* are
	# different facts and a caller drawing a month cannot tell them apart from the list alone.
	assert body["has_more"] is True

	# **Asked of the occurrence, answered about the series.** A person is always looking at the
	# instance, because the template is in no listing — so a client that had to find the
	# template first would be a client that cannot ask this at all.
	assert world.call("GET", "/v1/tasks").json()["items"][0]["ref"] == ref


def test_it_stops_where_the_caller_says_and_says_so (world: test_api_tasks.World) -> None:
	"""``until`` takes §9.3's grammar, and a bare day means the **end** of it.

	Stopping at the first instant of the last day drops an occurrence that falls on it, which
	reads as an off-by-one with no visible cause — the same rule §6.5 already applies to a
	deadline, arrived at from the other direction.
	"""

	made = world.call(
		"POST",
		"/v1/tasks",
		json={"text": "Stand up", "due": "2026-09-01", "recurrence": "every 7 days"},
	)
	ref = made.json()["ref"]

	body = world.call("GET", f"/v1/tasks/{ref}/occurrences?until=2026-09-22").json()

	assert [one[:10] for one in body["occurrences"]] == [
		"2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22",
	], body["occurrences"]

	# **The run ended because it ran out, not because the limit did**, which is the other half
	# of the pair above and the reason `has_more` is worth reporting at all.
	assert body["has_more"] is False


def test_asking_something_that_does_not_repeat_is_refused_by_name (
	world: test_api_tasks.World,
) -> None:
	"""Rather than answered with an empty list, which reads as *it never comes round*.

	An empty list is a plausible, complete, wrong answer — the shape this project keeps
	finding — and here it would be indistinguishable from a rule whose `UNTIL` has passed.
	"""

	made = world.call("POST", "/v1/tasks", json={"text": "Just the once"})
	ref = made.json()["ref"]

	answer = world.call("GET", f"/v1/tasks/{ref}/occurrences")

	assert answer.status_code == 404, answer.text
	assert "does not repeat" in answer.json()["detail"]
