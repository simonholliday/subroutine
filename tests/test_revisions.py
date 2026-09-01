"""Whether an item says its body has been replaced — `#1768`.

Decision `#1766` asks that a conclusion which changes be **edited into the body** rather than
corrected in a comment underneath. That rule was unfollowable as written, and the reason is
measurable rather than cultural: the old body was stored whole, ``subroutine changes`` said
*that* it changed, and ``show`` said nothing at all — so a fifth draft was indistinguishable
from a first on the surface people read. Editing was the invisible channel and commenting the
visible one, which is why anybody wanting their reasoning seen picked the comment.

**Driven over HTTP rather than against the domain**, because the question is what a *reader*
is told: the count is resolved by the single-item reads and by no listing, and a test calling
``revisions_of`` directly would prove the counting and say nothing about whether any surface
asks for it.
"""

import datetime
import typing

import pytest
import sqlalchemy.orm

import subroutine.domain.events
import subroutine.views
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction.

	Redeclared rather than imported: the fixture lives in ``tests/test_api_tasks.py`` and is
	not shared, so every file that wants one calls ``_world`` for itself.
	"""

	return test_api_tasks._world(session)


def _document (world: test_api_tasks.World, **fields: typing.Any) -> int:
	"""File a document with a body, and return its ref."""

	made = world.call(
		"POST", "/v1/documents", json={"title": "A conclusion", **fields}
	)

	assert made.status_code == 201, made.text

	return int(made.json()["ref"])


def _read (world: test_api_tasks.World, path: str) -> dict[str, typing.Any]:
	"""Read one item and return it."""

	answer = world.call("GET", path)

	assert answer.status_code == 200, answer.text

	return dict(answer.json())


def test_a_first_draft_says_nothing_about_revisions (
	world: test_api_tasks.World,
) -> None:
	"""§12.2a, one field along: an item nobody has revised has nothing to report.

	**Null rather than zero**, which is the same distinction `Page.held_back` draws: *never
	revised* and *nobody counted* are two answers, and a count of nothing would be a third
	thing that reads like the first.
	"""

	ref = _document(world, body="First draft.")

	assert _read(world, f"/v1/documents/{ref}")["revisions"] is None


def test_each_replacement_of_the_body_is_counted_and_attributed (
	world: test_api_tasks.World,
) -> None:
	"""The count, the name and the day, which is what `#1768` says and no more.

	**Not a diff.** Reading the superseded text is `#1428`'s job; two renderings of one
	history is the duplication this codebase keeps finding, so this answers only *has what I
	am reading been replaced*.
	"""

	ref = _document(world, body="First draft.")

	for words in ("Second draft.", "Third draft."):
		changed = world.call("PATCH", f"/v1/documents/{ref}", json={"body": words})

		assert changed.status_code == 200, changed.text

	revisions = _read(world, f"/v1/documents/{ref}")["revisions"]

	assert revisions is not None
	assert revisions["count"] == 2, revisions
	assert revisions["last_by"] == world.user.username
	assert revisions["last_at"] is not None


def test_writing_prose_for_the_first_time_is_not_a_revision (
	world: test_api_tasks.World,
) -> None:
	"""Found by driving it, and a document could not have shown it.

	A task captured from one line has no description, so the first ``PATCH`` recording one
	stores ``{"from": null, "to": …}``. Counting that said *revised twice* about something
	written once and corrected once. ``doc create --body`` writes a body at creation, so a
	document's first update genuinely is a replacement and this case never arose there.

	**Both halves in one test on purpose**: asserting only that the first write is uncounted
	would pass against a rule that counted nothing at all.
	"""

	made = world.call("POST", "/v1/tasks", json={"text": "Ordinary work"})

	assert made.status_code == 201, made.text

	ref = int(made.json()["ref"])

	world.call("PATCH", f"/v1/tasks/{ref}", json={"description": "A plan."})

	assert _read(world, f"/v1/tasks/{ref}")["revisions"] is None, (
		"writing a description onto a task that never had one is the first draft"
	)

	world.call("PATCH", f"/v1/tasks/{ref}", json={"description": "A better plan."})

	revisions = _read(world, f"/v1/tasks/{ref}")["revisions"]

	assert revisions is not None and revisions["count"] == 1, revisions


def test_a_rename_is_not_a_revision (world: test_api_tasks.World) -> None:
	"""`PROSE_FIELD` is narrower than `CONTENT_FIELDS`, and this is the difference driven.

	A title change is content — it changes what the item means — and a reader sees it, because
	the title is the thing they are looking at. What nothing said was that the *body* had
	moved underneath a comment written about an earlier one.
	"""

	ref = _document(world, body="First draft.")

	renamed = world.call("PATCH", f"/v1/documents/{ref}", json={"title": "Another name"})

	assert renamed.status_code == 200, renamed.text
	assert _read(world, f"/v1/documents/{ref}")["revisions"] is None


def test_a_listing_leaves_it_unresolved_rather_than_answering_never (
	world: test_api_tasks.World,
) -> None:
	"""**Null means nobody asked**, which is `Task.blocked_by`'s rule on the same model.

	Counting this per row is a scan a page does not currently make, and `#1764` measured that
	the agenda's own cost guard already bounds less than half of what a page issues — so a
	listing does not resolve it. A listing that answered ``0`` would be claiming something it
	had not looked up.
	"""

	ref = _document(world, body="First draft.")

	world.call("PATCH", f"/v1/documents/{ref}", json={"body": "Second draft."})

	assert _read(world, f"/v1/documents/{ref}")["revisions"] is not None, (
		"the single-item read resolves it"
	)

	listed = world.call("GET", "/v1/documents").json()["items"]
	found = [row for row in listed if row["ref"] == ref]

	assert found and found[0]["revisions"] is None, (
		"and the listing says nobody asked rather than never"
	)


def test_the_wording_is_one_sentence_every_surface_shares () -> None:
	"""`views.revised_in_words`, driven at its three shapes.

	**Singular, plural, and no actor.** The last is not decoration: an event records no actor
	for a migration or a caller with no credential (§12.1a), and *somebody revised this* is
	the whole of what is known — so a placeholder name would be a claim nothing supports.
	"""

	when = datetime.datetime(2026, 8, 31, tzinfo=datetime.UTC)

	once = subroutine.views.Revisions(count=1, last_at=when, last_by="si")
	twice = subroutine.views.Revisions(count=4, last_at=when, last_by="si")
	nameless = subroutine.views.Revisions(count=2, last_at=when, last_by=None)

	assert subroutine.views.revised_in_words(once, when="31 Aug") == (
		"revised once by @si on 31 Aug"
	)
	assert subroutine.views.revised_in_words(twice, when="31 Aug") == (
		"revised 4 times by @si on 31 Aug"
	)
	assert subroutine.views.revised_in_words(nameless, when="31 Aug") == (
		"revised 2 times on 31 Aug"
	)
