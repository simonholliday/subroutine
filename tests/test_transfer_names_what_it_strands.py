"""Handing an agent over names what nobody could see afterwards - `SR#5101`'s transfer half.

M-4 of the cold review of 2026-10-17, decided in `#5097`: a superuser handed ``bot`` - alice's agent,
owner of a private ``lab`` she saw only through it - to bob, who is in no workspace, and nobody could
see ``lab`` afterwards, while ``user transfer`` said only where the agent stops. **A transfer is never
refused for it**: it names, before and after, what it leaves without anybody, as deactivating
somebody does, asked of the instance with ``transferring`` and ``to``.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.domain.projects
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks
import test_stewardship

projects = subroutine.domain.projects

_agent = test_stewardship._agent
_as = test_stewardship._as
_metacortex = test_stewardship._metacortex
_person = test_stewardship._person
_project = test_stewardship._project


def _keys (rows: list[subroutine.domain.projects.Unreachable]) -> list[str]:
	"""Return the keys of the projects a listing names, in its order."""

	return [one.project.key for one in rows]


def test_handing_an_agent_over_names_what_nobody_could_see_once_it_moves (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The measured case: ``lab``, which only alice's ``bot`` is shared into, and ``nook``, which only an
	agent beneath ``bot`` is. **Both are named before the transfer to bob**, who is in no workspace, and
	listed after it; alice's own ``den``, which ``bot`` is shared into too, is not, nor ``attic``, which
	nobody could see already; and handing ``bot`` to carol, a member, strands nothing.
	"""

	thomas = _person(session, "thomas")
	alice = _person(session, "alice")
	carol = _person(session, "carol")
	bob = _person(session, "bob")
	gone = _person(session, "gone")
	bot = _agent(session, "bot", alice)
	sub = _agent(session, "sub", bot)
	workspace = _metacortex(
		session,
		thomas,
		alice=(alice, "member"),
		carol=(carol, "member"),
		gone=(gone, "member"),
		bot=(bot, "member"),
		sub=(sub, "member"),
	)
	_project(session, workspace, "lab", bot)
	_project(session, workspace, "nook", sub)
	_project(session, workspace, "attic", gone)
	den = _project(session, workspace, "den", alice)

	projects.share(session, den, bot, actor=_as(alice))
	subroutine.domain.users.set_active(session, gone, active=False)

	assert _keys(projects.unreachable(session)) == ["attic"]
	assert projects.unreachable(session, transferring=(bot, carol)) == []
	assert _keys(projects.unreachable(session, transferring=(bot, bob))) == ["lab", "nook"]

	subroutine.domain.users.transfer(session, bot, to=bob)

	assert _keys(projects.unreachable(session)) == ["attic", "lab", "nook"]


def test_the_question_before_a_transfer_is_refused_where_the_transfer_would_be (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The question asks the transfer's own checks first, so somebody about to make a transfer that
	would be refused is told so in the transfer's words, not told what it would strand."""

	thomas = _person(session, "thomas")
	alice = _person(session, "alice")
	bot = _agent(session, "bot", alice)
	_metacortex(session, thomas, alice=(alice, "member"), bot=(bot, "member"))

	for transferring, to, said in (
		(alice, thomas, f"{alice.username} is a person, and a person answers for themselves."),
		(bot, bot, f"{bot.username} already answers to {bot.username}"),
	):
		with pytest.raises(subroutine.errors.ValidationError) as refused:
			projects.unreachable(session, transferring=(transferring, to))

		assert refused.value.detail.startswith(said), refused.value.detail


def test_the_question_is_asked_over_http_with_transferring_and_to_and_refused_half_asked (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Decision `#5097`: ``transferring`` and ``to`` come together, and not beside ``leaving``."""

	world = test_api_tasks._world(session)
	alice = subroutine.domain.users.create(session, username=f"alice-{uuid.uuid4().hex[:6]}")
	bob = subroutine.domain.users.create(session, username=f"bob-{uuid.uuid4().hex[:6]}")
	bot = _agent(session, "bot", alice)

	for one in (alice, bot):
		subroutine.domain.workspaces.add_member(session, world.workspace, one, role_key="member")

	projects.create(
		session,
		workspace_id=world.workspace.id,
		key="lab",
		title="Lab",
		visibility="private",
		owner_id=bot.id,
	)
	session.flush()

	path = "/v1/instance/unreachable-projects"
	asked = world.call("GET", path, params={"transferring": bot.username, "to": bob.username})

	assert asked.status_code == 200, asked.text
	assert [row["project"] for row in asked.json()["items"]] == ["lab"], asked.text
	assert world.call("GET", path).json()["items"] == [], "nothing is stranded until it moves"

	for params, field in (
		({"transferring": bot.username}, "query.to"),
		({"to": bob.username}, "query.transferring"),
		({"transferring": bot.username, "to": bob.username, "leaving": alice.username}, "query.transferring"),
	):
		refused = world.call("GET", path, params=params)

		assert refused.status_code == 422, refused.text
		assert refused.json()["errors"][0]["field"] == field, refused.text
