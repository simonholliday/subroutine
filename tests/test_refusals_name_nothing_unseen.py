"""No refusal names a document or a task the caller cannot read - `SR#4574`, decision `#4521`.

S4 of the cold review of 2026-10-05, widened by its verification (area O): superseding a public
decision that a private document already supersedes was refused with the private document's ref and
title, and a hint to withdraw a link the caller could not see; restoring a successor named the hidden
ref too; and a ring refusal listed every ref in the ring. **One the caller cannot read is *something
you cannot see***, a ring keeps its length, and the hint offers only what the caller can do.
"""

import re
import typing

import pytest
import sqlalchemy.orm

import subroutine.domain.projects
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks
import test_document_binds
import test_sight_follows_the_chain

_member = test_document_binds._member
_as = test_document_binds._as
_written = test_document_binds._written


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


_person = test_sight_follows_the_chain._person
_metacortex = test_sight_follows_the_chain._metacortex
_project = test_sight_follows_the_chain._project
_principal = test_sight_follows_the_chain._as


def _whole (refused: subroutine.errors.SubroutineError) -> str:
	"""Return everything a refusal says - its sentence, its hint and its fields' messages."""

	return " ".join([refused.detail, refused.hint or "", *(error.message or "" for error in refused.errors)])


def test_taking_somebody_out_names_no_project_the_caller_cannot_see (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#5100`, H-4 of the cold review of 2026-10-17: the last-one refusal is said through reach.

	Carrieanne administers MetaCortex and cannot see keanu's private `severance`; taking keanu out
	is refused, as it strands it, without its key, and the hint names keanu, who can act on it. Her
	own private `web`, which keanu alone also sees, is named, since she can see it.
	"""

	carrieanne = _person(session, "carrieanne")
	keanu = _person(session, "keanu")
	workspace = _metacortex(session, carrieanne, keanu=(keanu, "member"))
	_project(session, workspace, "severance", keanu)
	session.flush()

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.workspaces.remove_member(session, workspace, keanu, actor=_principal(carrieanne))

	said = _whole(refused.value)

	assert "severance" not in said, said
	assert "something you cannot see" in said, said
	assert f"Ask {keanu.username} to share each" in (refused.value.hint or ""), refused.value.hint

	with pytest.raises(subroutine.errors.ValidationError) as asked_by_the_installation:
		subroutine.domain.workspaces.remove_member(session, workspace, keanu, actor=None)

	assert "severance" in asked_by_the_installation.value.detail, "an internal caller reads everything"


def test_unsharing_names_no_project_inside_that_the_caller_cannot_see (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#5100`: the unshare refusal for a private child is said through reach too.

	Jo owns private `outer`, shared with pat, and cannot see `severance` inside it, which only pat
	holds a row on. Taking pat's row on `outer` strands it, so it is refused - the review's Open
	Question 12, settled by #4943 rule 4 - without naming it, and the hint asks pat, who can see it.
	"""

	jo = _person(session, "jo")
	pat = _person(session, "pat")
	workspace = _metacortex(session, jo, pat=(pat, "member"))
	outer = _project(session, workspace, "outer", jo)
	subroutine.domain.projects.share(session, outer, pat)
	_project(session, workspace, "severance", pat, parent=outer)
	session.flush()

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.projects.unshare(session, outer, pat, actor=_principal(jo))

	said = _whole(refused.value)

	assert "severance" not in said, said
	assert said.count("something you cannot see") == 2, said
	assert refused.value.hint == f"Ask {pat.username} to share it with somebody else first."


def _names_none_of (text: str, *hidden: dict[str, typing.Any]) -> None:
	"""Fail if ``text`` names any of these rows, by its ref or by its title."""

	for row in hidden:
		assert not re.search(rf"#{row['ref']}(?!\d)", text), (row["ref"], text)
		assert row["title"] not in text, (row["title"], text)


def _vault (world: test_api_tasks.World) -> None:
	"""Make a private project only the world's own account is shared into."""

	made = world.call(
		"POST", "/v1/projects", json={"key": "vault", "title": "Vault", "visibility": "private"}
	)

	assert made.status_code == 201, made.text


def _supersedes (
	world: test_api_tasks.World, source: int, target: int, secret: str | None = None
) -> typing.Any:
	"""Say that one document replaces another, as the world's account or with a credential."""

	path = f"/v1/documents/{source}/links"
	body = {"target": target, "target_type": "document", "link_type": "supersedes"}

	return world.call("POST", path, json=body) if secret is None else _as(world, secret, "POST", path, json=body)


def test_a_second_successor_is_refused_without_naming_one_the_caller_cannot_see (
	world: test_api_tasks.World,
) -> None:
	"""The refusal on linking: keanu cannot see the private replacement, and was told its ref and title.

	**And the control**: the owner, who reads it, is still told which one, and how to put it right.
	"""

	_keanu, member = _member(world, "keanu", "member")
	_vault(world)
	decision = _written(world, title="Ship on Thursdays")
	hidden = _written(world, title="Ship on Fridays, quietly", project="vault")

	assert _supersedes(world, hidden["ref"], decision["ref"]).status_code == 201

	mine = _written(world, member, title="Ship on Wednesdays")
	refused = _supersedes(world, mine["ref"], decision["ref"], member)

	assert refused.status_code == 409, refused.text

	_names_none_of(refused.text, hidden)

	assert "something you cannot see" in refused.json()["detail"], refused.text
	assert "Withdraw that link" not in refused.text, refused.text

	told = _supersedes(world, mine["ref"], decision["ref"])

	assert told.status_code == 409, told.text
	assert f"#{hidden['ref']}" in told.json()["detail"], told.text
	assert "Withdraw that link first" in told.json()["hint"], told.text


def test_restoring_a_successor_is_refused_without_naming_one_the_caller_cannot_see (
	world: test_api_tasks.World,
) -> None:
	"""The refusal on restoring: the place keanu's revision held was taken by a private one meanwhile.

	keanu administers the workspace and is not shared into the private project, which hides it from
	him as from anybody (decision `#4383`).
	"""

	_keanu, admin = _member(world, "keanu", "admin")
	_vault(world)
	decision = _written(world, title="Ship on Thursdays")
	mine = _written(world, admin, title="Ship on Wednesdays")

	assert _supersedes(world, mine["ref"], decision["ref"], admin).status_code == 201
	assert _as(world, admin, "DELETE", f"/v1/documents/{mine['ref']}").status_code < 300

	hidden = _written(world, title="Ship on Fridays, quietly", project="vault")

	assert _supersedes(world, hidden["ref"], decision["ref"]).status_code == 201

	refused = _as(world, admin, "POST", f"/v1/documents/{mine['ref']}/restore")

	assert refused.status_code == 409, refused.text

	_names_none_of(refused.text, hidden)

	assert "something you cannot see" in refused.json()["detail"], refused.text


def test_a_ring_keeps_its_length_and_shows_a_step_the_caller_cannot_see_as_unseen (
	world: test_api_tasks.World,
) -> None:
	"""A ring refusal listed every ref in the chain, a private task's included.

	**Its length is kept**, since a ring of three and a ring of two are different things to untangle,
	and the step keanu cannot read is said as one. The owner is told every ref.
	"""

	_keanu, member = _member(world, "keanu", "member")
	_vault(world)

	def task (title: str, **body: typing.Any) -> dict[str, typing.Any]:
		"""Make a task as the world's own account."""

		made = world.call("POST", "/v1/tasks", json={"title": title, **body})

		assert made.status_code == 201, made.text

		return dict(made.json())

	first = task("Book the venue")
	hidden = task("Pay the deposit, quietly", project="vault")
	last = task("Send the invitations")

	for source, target in ((first, hidden), (hidden, last)):
		linked = world.call(
			"POST",
			f"/v1/tasks/{source['ref']}/links",
			json={"target_type": "task", "target": target["ref"], "link_type": "blocks"},
		)

		assert linked.status_code == 201, linked.text

	closing = {"target_type": "task", "target": first["ref"], "link_type": "blocks"}
	refused = _as(world, member, "POST", f"/v1/tasks/{last['ref']}/links", json=closing)

	assert refused.status_code == 409, refused.text
	assert refused.json()["code"] == "cycle_detected", refused.text

	_names_none_of(refused.text, hidden)

	said = refused.json()["errors"][0]["message"]

	assert said == (
		f"The chain that comes back is #{first['ref']} → something you cannot see → #{last['ref']}."
	), said

	# **Every link in it has an end keanu cannot see**, so withdrawing one is not his to offer
	# (`SR#5106`); the owner, who sees them all, is offered it.
	assert "Withdraw a link in that chain" not in refused.json()["hint"], refused.text
	assert "can see both ends of a link in that chain" in refused.json()["hint"], refused.text

	told = world.call("POST", f"/v1/tasks/{last['ref']}/links", json=closing)

	assert f"#{hidden['ref']}" in told.json()["errors"][0]["message"], told.text
	assert "Withdraw a link in that chain" in told.json()["hint"], told.text


def test_a_ring_with_one_link_in_sight_offers_withdrawing_it (
	world: test_api_tasks.World,
) -> None:
	"""keanu sees both ends of one link in the ring, so he may withdraw that one, and is offered it.

	`SR#5106`: withdrawal is withheld only where *no* link has both ends in sight, so a ring that
	runs through something unseen and also through two things keanu reads is still his to break.
	"""

	_keanu, member = _member(world, "keanu", "member")
	_vault(world)

	def task (title: str, **body: typing.Any) -> dict[str, typing.Any]:
		"""Make a task as the world's own account."""

		made = world.call("POST", "/v1/tasks", json={"title": title, **body})

		assert made.status_code == 201, made.text

		return dict(made.json())

	first = task("Book the venue")
	second = task("Choose the date")
	hidden = task("Pay the deposit, quietly", project="vault")
	last = task("Send the invitations")

	for source, target in ((first, second), (second, hidden), (hidden, last)):
		linked = world.call(
			"POST",
			f"/v1/tasks/{source['ref']}/links",
			json={"target_type": "task", "target": target["ref"], "link_type": "blocks"},
		)

		assert linked.status_code == 201, linked.text

	closing = {"target_type": "task", "target": first["ref"], "link_type": "blocks"}
	refused = _as(world, member, "POST", f"/v1/tasks/{last['ref']}/links", json=closing)

	assert refused.status_code == 409, refused.text
	assert "Withdraw a link in that chain" in refused.json()["hint"], refused.text
