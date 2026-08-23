"""What an item's writing suggests, and what it deliberately does not — `#1137`.

*What governs this* answers from typed links alone: **near** is not **binds**, and answering
the second under the first's name spends the trust the feature exists to earn. The cost of
that decision is a cold start — on a fresh install nothing is typed, so the answer is empty
for ever unless somebody happens to reach for a link type they have never been shown.

A citation is the evidence that already exists. Somebody wrote `#1131` in their own
description, deliberately. So this proposes the link and lets a person or an agent confirm it,
and the answer stays typed-links-only exactly as decided.

**Every test here is about a boundary rather than the happy path**, because the failure mode
is not *no proposals* — it is proposals nobody agrees with, which is worse than none.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.vocabulary
import subroutine.domain.authentication
import subroutine.domain.documents
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP."""

	return test_api_tasks._world(session)


def _document (
	world: test_api_tasks.World, *, kind: str = "decision", title: str = "The decision"
) -> dict[str, typing.Any]:
	"""Write a document of one type."""

	response = world.call(
		"POST", "/v1/documents", json={"title": title, "body": "Because.", "type": kind}
	)

	assert response.status_code == 201, response.text

	return typing.cast(dict[str, typing.Any], response.json())


def _task (world: test_api_tasks.World, **body: typing.Any) -> dict[str, typing.Any]:
	"""Make a task."""

	body.setdefault("title", "The work")
	response = world.call("POST", "/v1/tasks", json=body)

	assert response.status_code == 201, response.text

	return typing.cast(dict[str, typing.Any], response.json())


def _proposed (
	world: test_api_tasks.World, ref: int, *, kind: str = "tasks"
) -> list[dict[str, typing.Any]]:
	"""Read what this item's writing suggests."""

	response = world.call("GET", f"/v1/{kind}/{ref}/proposed-links")

	assert response.status_code == 200, response.text

	return typing.cast(list[dict[str, typing.Any]], response.json()["items"])


def test_a_citation_of_a_decision_proposes_the_link_that_would_make_it_governing (
	world: test_api_tasks.World,
) -> None:
	"""The whole feature in one case."""

	decision = _document(world)
	work = _task(world, description=f"Follows #{decision['ref']}.")
	found = _proposed(world, work["ref"])

	assert [one["other"]["ref"] for one in found] == [decision["ref"]]
	assert found[0]["link_type"] == "documents"
	assert found[0]["direction"] == "incoming", "the document governs the task, not the reverse"
	assert found[0]["because"] == "this names it"
	assert "id" not in found[0], "a proposal is not a link and must not look like one"


def test_confirming_a_proposal_is_an_ordinary_link_and_the_proposal_goes (
	world: test_api_tasks.World,
) -> None:
	"""There is no confirm verb, deliberately.

	A confirmed proposal has to be indistinguishable from a link somebody made by hand, or
	the graph grows two kinds of edge and every reader has to know which it is looking at.
	So confirming is ``POST .../links`` with what the proposal names, and what makes the
	proposal disappear is the link existing rather than anything recording the act.
	"""

	decision = _document(world)
	work = _task(world, description=f"Follows #{decision['ref']}.")
	proposal = _proposed(world, work["ref"])[0]

	made = world.call(
		"POST",
		f"/v1/documents/{decision['ref']}/links",
		json={
			"target": work["ref"],
			"target_type": "task",
			"link_type": proposal["link_type"],
		},
	)

	assert made.status_code == 201, made.text
	assert _proposed(world, work["ref"]) == []

	links = world.call("GET", f"/v1/tasks/{work['ref']}/links").json()["items"]

	assert [one["label"] for one in links] == [proposal["label"]]


def test_a_pair_already_joined_by_any_link_is_not_proposed_again (
	world: test_api_tasks.World,
) -> None:
	"""Somebody has already looked at this pair, and arguing with them is not a proposal.

	The exclusion is any link of any type rather than a ``documents`` one, which is the
	stronger rule and the deliberate one: two items already related have been thought about,
	and proposing an edge over the top of that answer says the thinking did not count.
	"""

	decision = _document(world)
	work = _task(world, description=f"Follows #{decision['ref']}.")

	world.call(
		"POST",
		f"/v1/tasks/{work['ref']}/links",
		json={
			"target": decision["ref"],
			"target_type": "document",
			"link_type": "relates_to",
		},
	)

	assert _proposed(world, work["ref"]) == []


@pytest.mark.parametrize("kind", sorted(subroutine.domain.documents.DESCRIBES))
def test_a_document_that_describes_rather_than_binds_is_never_proposed (
	world: test_api_tasks.World, kind: str
) -> None:
	"""A finding is a conclusion about something, not a rule it has to follow.

	Parametrised over the *register* rather than over a written list, so a seventh document
	type classified as describing is covered on the day it is classified.
	"""

	described = _document(world, kind=kind, title="What we found")
	work = _task(world, description=f"See #{described['ref']}.")

	assert _proposed(world, work["ref"]) == []


@pytest.mark.parametrize("kind", sorted(subroutine.domain.documents.GOVERNS))
def test_every_governing_type_is_proposed (
	world: test_api_tasks.World, kind: str
) -> None:
	"""And the other half of the same register, so neither can drift alone."""

	governing = _document(world, kind=kind, title="The rule")
	work = _task(world, description=f"Follows #{governing['ref']}.")

	assert [one["other"]["ref"] for one in _proposed(world, work["ref"])] == [
		governing["ref"]
	]


def test_a_citation_of_a_task_is_never_proposed (world: test_api_tasks.World) -> None:
	"""A task cannot govern anything. Only a document is a written rule."""

	first = _task(world, title="The first")
	second = _task(world, title="The second", description=f"After #{first['ref']}.")

	assert _proposed(world, second["ref"]) == []


def test_a_citation_in_a_comment_is_proposed_and_says_where_it_was (
	world: test_api_tasks.World,
) -> None:
	"""A comment resolves to the item it is on, because a comment has no ref to open."""

	decision = _document(world)
	work = _task(world)
	world.call(
		"POST",
		f"/v1/tasks/{work['ref']}/comments",
		json={"body": f"This is settled by #{decision['ref']}."},
	)
	found = _proposed(world, work["ref"])

	assert [one["other"]["ref"] for one in found] == [decision["ref"]]
	assert found[0]["because"] == "a comment here names it"


def test_the_documents_own_words_propose_it_too (world: test_api_tasks.World) -> None:
	"""Evidence runs both ways: a decision naming the work it governs is a citation as well."""

	work = _task(world)
	decision = _document(world)
	world.call(
		"PATCH",
		f"/v1/documents/{decision['ref']}",
		json={"body": f"This governs #{work['ref']}."},
	)
	found = _proposed(world, work["ref"])

	assert [one["other"]["ref"] for one in found] == [decision["ref"]]
	assert found[0]["because"] == "it names this"


def test_an_items_own_words_outrank_a_comment_when_both_cite_the_same_thing (
	world: test_api_tasks.World,
) -> None:
	"""One pair, one proposal, and the evidence quoted is the strongest there is.

	Without an order the description would be whichever row the database returned first,
	which is a sentence about the pair that changes between runs.
	"""

	decision = _document(world)
	work = _task(world, description=f"Follows #{decision['ref']}.")
	world.call(
		"POST",
		f"/v1/tasks/{work['ref']}/comments",
		json={"body": f"Still following #{decision['ref']}."},
	)
	found = _proposed(world, work["ref"])

	assert len(found) == 1, "one pair is one proposal, however many times it was cited"
	assert found[0]["because"] == "this names it"


def test_a_document_is_asked_what_its_writing_suggests_it_governs (
	world: test_api_tasks.World,
) -> None:
	"""The same question from the other end, which is how a decision finds its work."""

	decision = _document(world)
	work = _task(world, description=f"Follows #{decision['ref']}.")
	found = _proposed(world, decision["ref"], kind="documents")

	assert [one["other"]["ref"] for one in found] == [work["ref"]]
	assert found[0]["direction"] == "outgoing", "this document governs that task"
	assert found[0]["because"] == "it names this"


def test_a_governing_document_at_both_ends_proposes_nothing (
	world: test_api_tasks.World,
) -> None:
	"""Two decisions citing each other is the shape where a citation least often means *binds*.

	It usually means *this replaces that* or *this argues with that* — and ``supersedes``
	already says the first properly, with an integrity rule this could not have. Silence is
	the right answer where the evidence supports two opposite readings equally.
	"""

	first = _document(world, title="The first rule")
	second = _document(world, title="The second rule")
	world.call(
		"PATCH",
		f"/v1/documents/{second['ref']}",
		json={"body": f"Unlike #{first['ref']}."},
	)

	assert _proposed(world, second["ref"], kind="documents") == []
	assert _proposed(world, first["ref"], kind="documents") == []


def test_a_finding_can_be_governed_even_though_it_cannot_govern (
	world: test_api_tasks.World,
) -> None:
	"""The register decides what *binds*, not what may *be bound*.

	A finding is not a rule, so it never appears as the governing end. It is still a piece of
	writing a decision can settle, so it appears as the governed one — and treating it like a
	task here is what keeps the rule one rule rather than a list of entity types.
	"""

	decision = _document(world, title="The rule")
	finding = _document(world, kind="finding", title="What we found")
	world.call(
		"PATCH",
		f"/v1/documents/{finding['ref']}",
		json={"body": f"Follows #{decision['ref']}."},
	)
	found = _proposed(world, finding["ref"], kind="documents")

	assert [one["other"]["ref"] for one in found] == [decision["ref"]]
	assert found[0]["direction"] == "incoming"

	from_the_rule = _proposed(world, decision["ref"], kind="documents")

	assert [one["other"]["ref"] for one in from_the_rule] == [finding["ref"]]
	assert from_the_rule[0]["direction"] == "outgoing"


def test_a_document_the_reader_cannot_see_is_not_proposed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§6.15's rule, applied to a proposal rather than to a mention.

	A citation from somewhere invisible is **omitted entirely** rather than reported as
	hidden: *something you cannot see says this binds you* discloses that the document exists
	and explains nothing. A proposal is worse than a backlink here, because it names an item
	the reader would then be invited to link to.
	"""

	world = test_api_tasks._world(session)
	world.call(
		"POST",
		"/v1/projects",
		json={"key": "secret", "title": "Secret", "visibility": "private"},
	)
	hidden = world.call(
		"POST",
		"/v1/documents",
		json={"title": "The private rule", "type": "decision", "project": "secret"},
	).json()
	work = world.call(
		"POST", "/v1/tasks", json={"title": "The work", "description": f"Follows #{hidden['ref']}."}
	).json()

	assert [one["other"]["ref"] for one in _proposed(world, work["ref"])] == [hidden["ref"]], (
		"the owner sees it, so this proves the citation was indexed at all"
	)

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(
		session, world.workspace, outsider, role_key="member"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()
	nosy = world._replace(secret=issued.value.get_secret_value())

	assert _proposed(nosy, work["ref"]) == []


def test_a_workspace_that_has_removed_the_link_type_is_proposed_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#826` made the vocabulary editable, and a removed type is a statement.

	Offering a link a workspace cannot make would be offering work that refuses — and the
	refusal would name a link type the reader has deliberately deleted, which reads as a bug
	in the program rather than as a consequence of their own configuration.
	"""

	world = test_api_tasks._world(session)
	decision = _document(world)
	work = _task(world, description=f"Follows #{decision['ref']}.")

	assert _proposed(world, work["ref"]) != []

	kinds = world.call("GET", "/v1/link-types").json()["items"]
	governing = next(one for one in kinds if one["key"] == "documents")
	removed = world.call("DELETE", f"/v1/link-types/{governing['id']}")

	assert removed.status_code == 204, removed.text
	assert _proposed(world, work["ref"]) == []
