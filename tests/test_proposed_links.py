"""What governs an item, and what its writing merely suggests — `#1119` and `#1137`.

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
import subroutine.views
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


def test_one_citation_proposes_one_edge_whichever_end_asks_about_it (
	world: test_api_tasks.World,
) -> None:
	"""`SR#1609`. One citation is one link, and reading it from the far end cannot reverse it.

	**The property the surfaces get wrong, stated where both of them can be held to it.** A
	renderer builds a confirming command from a proposal, and both built it as *the other end,
	then this one* — a fixed order that is right only while the far end is the governing
	document. Asked from the document, the same order proposes that the work documents the
	specification.

	**Asserted as an equality between the two answers rather than against a literal**, because
	a literal would let both ends drift together. What must hold is that one citation describes
	one edge however it is read, and that its source is the document that governs.
	"""

	decision = _document(world)
	work = _task(world, description=f"Follows #{decision['ref']}.")

	asked_of_the_work = _proposed(world, work["ref"])
	asked_of_the_decision = _proposed(world, decision["ref"], kind="documents")

	assert len(asked_of_the_work) == 1, asked_of_the_work
	assert len(asked_of_the_decision) == 1, asked_of_the_decision

	near_work = subroutine.views.Proposal.model_validate(asked_of_the_work[0])
	near_decision = subroutine.views.Proposal.model_validate(asked_of_the_decision[0])

	from_the_work = near_work.confirmed_as(work["ref"])
	from_the_decision = near_decision.confirmed_as(decision["ref"])

	assert from_the_work == (decision["ref"], work["ref"]), (
		f"the document governs, so it is the source: {from_the_work}"
	)
	assert from_the_work == from_the_decision, (
		"one citation is one edge, and which end asked may not decide which way it runs"
	)

	# **The two ends really are reading different `direction` values**, so the equality above
	# is a rule being applied rather than two identical inputs agreeing. Without this the test
	# would still pass against a version that ignored `direction` entirely on both paths.
	assert near_work.direction != near_decision.direction, (
		"if both ends report one direction this test is proving nothing"
	)


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


def test_a_workspace_that_has_renamed_the_link_type_is_proposed_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The second of three ways a workspace says it does not use this relation.

	Kept apart from the re-categorised case below, because the two look identical from the
	outside and only one of them was ever right. A renamed key has always produced no
	proposals; a re-categorised one produced proposals that governed nothing.
	"""

	world = test_api_tasks._world(session)
	decision = _document(world)
	work = _task(world, description=f"Follows #{decision['ref']}.")

	assert _proposed(world, work["ref"]) != []

	kinds = world.call("GET", "/v1/link-types").json()["items"]
	governing = next(one for one in kinds if one["key"] == "documents")
	renamed = world.call(
		"PATCH", f"/v1/link-types/{governing['id']}", json={"key": "settles"}
	)

	assert renamed.status_code == 200, renamed.text
	assert _proposed(world, work["ref"]) == []


def test_a_relation_that_no_longer_governs_is_not_proposed_as_one_that_does (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#1166`, found by a cold review asking whether `#1157`'s one exception survives a reader.

	It survives about **direction** and did not survive about **meaning**. ``proposals`` reads
	the key because only a key can say which end a document goes at — but it read the key
	*instead of* the category, so a workspace that re-categorised this relation was offered a
	proposal, confirmed it because the product suggested it, and got a link that
	:func:`~subroutine.domain.links.governing` then ignored.

	**The two halves are asserted together on purpose.** Either one alone passes against the
	defect: before the fix the proposal was offered *and* the link did not govern, and each of
	those is a true statement about a working feature in some other configuration. What is
	wrong is the pair.
	"""

	world = test_api_tasks._world(session)
	decision = _document(world)
	work = _task(world, description=f"Follows #{decision['ref']}.")

	assert _proposed(world, work["ref"]) != [], "the citation is indexed and offered"

	kinds = world.call("GET", "/v1/link-types").json()["items"]
	governing = next(one for one in kinds if one["key"] == "documents")
	moved = world.call(
		"PATCH", f"/v1/link-types/{governing['id']}", json={"category": "describing"}
	)

	assert moved.status_code == 200, moved.text
	assert moved.json()["category"] == "describing"

	assert _proposed(world, work["ref"]) == [], (
		"a relation that no longer binds must not be proposed as one that does"
	)

	# And the other half: confirming it by hand still produces a link that governs nothing, so
	# the proposal would have been advice the product then declined to honour.
	made = world.call(
		"POST",
		f"/v1/documents/{decision['ref']}/links",
		json={"target": work["ref"], "target_type": "task", "link_type": "documents"},
	)

	assert made.status_code == 201, made.text
	assert _governing(world, work["ref"]) == []


def _governing (
	world: test_api_tasks.World, ref: int, *, kind: str = "tasks"
) -> list[dict[str, typing.Any]]:
	"""Read what is in force over this item."""

	response = world.call("GET", f"/v1/{kind}/{ref}/governing")

	assert response.status_code == 200, response.text

	return typing.cast(list[dict[str, typing.Any]], response.json()["items"])


def test_a_documents_link_is_what_makes_a_decision_govern (
	world: test_api_tasks.World,
) -> None:
	"""`#1119`. The whole feature, and the whole of what it is allowed to answer from."""

	decision = _document(world, title="How dates are written")
	work = _task(world)

	assert _governing(world, work["ref"]) == [], "nothing binds it until somebody says so"

	world.call(
		"POST",
		f"/v1/documents/{decision['ref']}/links",
		json={"target": work["ref"], "target_type": "task", "link_type": "documents"},
	)
	found = _governing(world, work["ref"])

	assert [one["document"]["ref"] for one in found] == [decision["ref"]]
	assert found[0]["link_type"] == "documents"
	assert found[0]["document"]["title"] == "How dates are written"
	assert "body" not in found[0]["document"], "titles and refs, never bodies"


def test_deriving_from_a_specification_is_the_other_way_to_say_it (
	world: test_api_tasks.World,
) -> None:
	"""§5.7's own example: write a specification, then the tasks that implement it.

	**The specification has to be agreed first**, which is `#506`'s rule and is not an
	accident of this fixture: a `decision`, a `finding` and a `dead_end` start in force
	because writing one *is* the act, and a `spec` starts as a draft because a specification
	nobody has agreed to is a proposal. So a draft specification governs nothing, and this
	activates it deliberately rather than working around it.
	"""

	spec = _document(world, kind="spec", title="What the parser accepts")
	world.call("PATCH", f"/v1/documents/{spec['ref']}", json={"status": "active"})
	work = _task(world)
	world.call(
		"POST",
		f"/v1/tasks/{work['ref']}/links",
		json={
			"target": spec["ref"],
			"target_type": "document",
			"link_type": "derives_from",
		},
	)
	found = _governing(world, work["ref"])

	assert [one["link_type"] for one in found] == ["derives_from"]


@pytest.mark.parametrize("relation", ["relates_to", "blocks", "duplicates"])
def test_being_merely_related_to_a_decision_is_not_being_governed_by_it (
	world: test_api_tasks.World, relation: str
) -> None:
	"""`#1124` Q2, and this is the test that holds it.

	*Near this* and *binds this* are different claims, and a feature answering the second
	while showing the first teaches a reader to distrust it. Every other seeded relation is
	driven, so a new one is not quietly admitted.
	"""

	decision = _document(world)
	work = _task(world)
	made = world.call(
		"POST",
		f"/v1/tasks/{work['ref']}/links",
		json={
			"target": decision["ref"],
			"target_type": "document",
			"link_type": relation,
		},
	)

	assert made.status_code == 201, made.text
	assert _governing(world, work["ref"]) == []


def test_a_superseded_decision_stops_governing (world: test_api_tasks.World) -> None:
	"""`#1036`'s rule: it asks whether a document is in force, not what type it is.

	A rule that has been replaced is not a rule, and a reading list that still names it sends
	somebody to do the thing the newer decision reversed. This is the single most damaging
	way for the answer to be wrong, because it is confidently wrong.
	"""

	decision = _document(world, title="The old rule")
	work = _task(world)
	world.call(
		"POST",
		f"/v1/documents/{decision['ref']}/links",
		json={"target": work["ref"], "target_type": "task", "link_type": "documents"},
	)

	assert _governing(world, work["ref"]) != []

	replacement = _document(world, title="The new rule")
	retired = world.call(
		"PATCH",
		f"/v1/documents/{replacement['ref']}",
		json={"supersedes": decision["ref"]},
	)

	assert retired.status_code == 200, retired.text
	assert _governing(world, work["ref"]) == [], "a superseded decision is not in force"


def test_a_draft_decision_does_not_govern_yet (world: test_api_tasks.World) -> None:
	"""The other end of the same rule, and the one a reader is most likely to disagree with.

	A decision written and not yet agreed is a proposal. Listing it under *read first* would
	have somebody follow a rule nobody has taken.
	"""

	decision = _document(world, title="The proposed rule")
	work = _task(world)
	world.call(
		"POST",
		f"/v1/documents/{decision['ref']}/links",
		json={"target": work["ref"], "target_type": "task", "link_type": "documents"},
	)

	assert _governing(world, work["ref"]) != []
	assert (
		world.call(
			"PATCH", f"/v1/documents/{decision['ref']}", json={"status": "draft"}
		).status_code
		== 200
	)
	assert _governing(world, work["ref"]) == []


@pytest.mark.parametrize("kind", sorted(subroutine.domain.documents.DESCRIBES))
def test_a_document_that_describes_does_not_govern_even_when_linked (
	world: test_api_tasks.World, kind: str
) -> None:
	"""A `derives_from` link to a finding is a real relationship and a different question.

	§5.7's own second example is a bug deriving from the failing check that found it — which
	is exactly this shape, and is emphatically not a rule the bug has to follow.
	"""

	described = _document(world, kind=kind, title="What we found")
	work = _task(world)
	world.call(
		"POST",
		f"/v1/tasks/{work['ref']}/links",
		json={
			"target": described["ref"],
			"target_type": "document",
			"link_type": "derives_from",
		},
	)

	assert world.call("GET", f"/v1/tasks/{work['ref']}/links").json()["items"], (
		"the link was not made, so this proves nothing"
	)
	assert _governing(world, work["ref"]) == []


def test_a_governing_document_the_reader_cannot_see_is_not_named (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#856`'s third finding: inheriting anything through a graph is a disclosure.

	A reading list is the worst place to leak one, because it does not merely say a document
	exists — it says a document the reader cannot open is a rule they are being held to.
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
	work = _task(world)
	world.call(
		"POST",
		f"/v1/documents/{hidden['ref']}/links",
		json={"target": work["ref"], "target_type": "task", "link_type": "documents"},
	)

	assert _governing(world, work["ref"]) != [], "the owner sees it, so the link was made"

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(
		session, world.workspace, outsider, role_key="member"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()
	nosy = world._replace(secret=issued.value.get_secret_value())

	assert _governing(nosy, work["ref"]) == []


def test_a_document_says_what_governs_it_too (world: test_api_tasks.World) -> None:
	"""A design is bound by the decision that settled it, exactly as work is."""

	decision = _document(world, title="The rule")
	design = _document(world, kind="design", title="How it was built")
	world.call(
		"POST",
		f"/v1/documents/{decision['ref']}/links",
		json={
			"target": design["ref"],
			"target_type": "document",
			"link_type": "documents",
		},
	)
	found = _governing(world, design["ref"], kind="documents")

	assert [one["document"]["ref"] for one in found] == [decision["ref"]]


def test_the_reading_list_is_newest_first (world: test_api_tasks.World) -> None:
	"""Ref descending, which is creation order within a workspace and is deterministic.

	`created_at` is not: two documents written in one transaction share an instant, and a
	reading list whose order changed between reads would look like the answer changing.
	"""

	work = _task(world)
	refs = []

	for name in ("First", "Second", "Third"):
		made = _document(world, title=name)
		refs.append(made["ref"])
		world.call(
			"POST",
			f"/v1/documents/{made['ref']}/links",
			json={"target": work["ref"], "target_type": "task", "link_type": "documents"},
		)

	assert [one["document"]["ref"] for one in _governing(world, work["ref"])] == sorted(
		refs, reverse=True
	)
