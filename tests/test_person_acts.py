"""A person's acts are one list, refused to an agent first, whatever it holds - `SR#4565`.

Decision `#4515`, on S6, S7 and A I-10 of the cold review of 2026-10-05: the rule lived in seven
inline checks with two status codes and two orders, each of 0.10.0's four fixes patching one of
them, and an administering agent still issued a working credential for another person's agent,
signed a person out everywhere, and made itself owner where no owner could act - because that one
check ran after the superuser bypass.

So **every act on the list is driven here as a superuser agent**, which holds everything a
credential can, and refused in one sentence; the list and the drivers must name the same acts, so
an act added to the list without a driver fails. And **nothing outside the guard asks whether an
account is an agent to decide what it may do**, which the scan at the bottom holds.
"""

import ast
import dataclasses
import pathlib
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.documents
import subroutine.domain.sessions
import subroutine.domain.tokens
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions

Principal = subroutine.domain.authentication.Principal

DOMAIN = pathlib.Path(__file__).resolve().parent.parent / "src" / "subroutine" / "domain"


@dataclasses.dataclass
class Cast:
	"""A workspace whose only owner has left, a superuser person, and a superuser agent of theirs."""

	workspace: subroutine.db.models.identity.Workspace
	laurence: subroutine.db.models.identity.User
	carrie_anne: subroutine.db.models.identity.User
	dozer: subroutine.db.models.identity.User
	tank: subroutine.db.models.identity.User


def _cast (session: sqlalchemy.orm.Session) -> Cast:
	"""Set the cast up: ``dozer`` administers the installation for ``laurence``, ``tank`` answers to it."""

	keanu = subroutine.domain.users.create(session, username="keanu")
	workspace = subroutine.domain.workspaces.create(
		session, slug="metacortex", title="Metacortex", owner=keanu, timezone="UTC"
	)
	laurence = subroutine.domain.users.create(session, username="laurence", is_superuser=True)
	carrie_anne = subroutine.domain.users.create(session, username="carrie-anne")
	dozer = subroutine.domain.users.create(
		session,
		username="dozer",
		is_service_account=True,
		is_superuser=True,
		responsible_user_id=laurence.id,
	)
	tank = subroutine.domain.users.create(
		session, username="tank", is_service_account=True, actor=Principal(user=dozer)
	)

	for member, role in ((carrie_anne, "admin"), (dozer, "admin")):
		subroutine.domain.workspaces.add_member(session, workspace, member, role_key=role)

	# The only owner has left, which is what makes an owner a repair rather than a promotion.
	keanu.is_active = False
	session.flush()

	return Cast(
		workspace=workspace, laurence=laurence, carrie_anne=carrie_anne, dozer=dozer, tank=tank
	)


def _inbox (
	session: sqlalchemy.orm.Session, workspace: subroutine.db.models.identity.Workspace
) -> subroutine.db.models.project.Project:
	"""Return the workspace's Inbox, which every workspace is made with."""

	model = subroutine.db.models.project.Project

	return session.scalars(
		sqlalchemy.select(model).where(model.workspace_id == workspace.id, model.key == "inbox")
	).one()


def _drivers (
	session: sqlalchemy.orm.Session, cast: Cast
) -> dict[str, typing.Callable[[], object]]:
	"""Return each person's act, taken by the superuser agent - or for a session, held by it."""

	as_dozer = Principal(user=cast.dozer)
	name = f"made-{uuid.uuid4().hex[:8]}"
	permissions = subroutine.permissions

	return {
		permissions.MAKING_A_SUPERUSER: lambda: subroutine.domain.users.create(
			session, username=name, is_service_account=True, is_superuser=True, actor=as_dozer
		),
		permissions.CHANGING_STANDING: lambda: subroutine.domain.users.set_active(
			session, cast.carrie_anne, active=False, actor=as_dozer
		),
		permissions.HANDING_AN_AGENT_OVER: lambda: subroutine.domain.users.transfer(
			session, cast.tank, to=cast.laurence, actor=as_dozer
		),
		permissions.NAMING_AN_ANSWERER: lambda: subroutine.domain.users.create(
			session,
			username=name,
			is_service_account=True,
			responsible_user_id=cast.laurence.id,
			actor=as_dozer,
		),
		permissions.CREATING_A_PERSON: lambda: subroutine.domain.users.create(
			session, username=name, actor=as_dozer
		),
		permissions.ISSUING: lambda: subroutine.domain.authentication.issue_token(
			session, user=cast.carrie_anne, title="As them", actor=as_dozer
		),
		permissions.STOPPING: lambda: subroutine.domain.sessions.sign_out_everywhere(
			session, user=cast.carrie_anne, actor=as_dozer
		),
		permissions.MAKING_AN_OWNER_WHERE_NONE_CAN_ACT: (
			lambda: subroutine.domain.workspaces.set_member_role(
				session, cast.workspace, cast.dozer, role_key="owner", actor=as_dozer
			)
		),
		permissions.NAMING_A_MAINTAINER: lambda: subroutine.domain.documents.create(
			session,
			project=_inbox(session, cast.workspace),
			title="Who maintains this",
			owner_id=cast.carrie_anne.id,
			actor=as_dozer,
		),
		permissions.HOLDING_A_BROWSER_SESSION: lambda: subroutine.domain.sessions.mint_link(
			session, user=cast.dozer, actor=Principal(user=cast.laurence)
		),
	}


def test_every_person_act_is_refused_to_an_agent_in_one_sentence (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Each act on the list, as an agent that administers the installation: one refusal, 403.

	**The superuser agent is the point**: it holds every instance verb and bypasses every role, so
	anything that lets it through is an act checked after the bypass, which is how G3 happened.
	"""

	cast = _cast(session)
	drivers = _drivers(session, cast)

	assert set(drivers) == set(subroutine.permissions.PERSON_ACTS), (
		"every person's act needs a driver here, and every driver an act on the list"
	)

	for act, driver in drivers.items():
		with pytest.raises(subroutine.errors.Forbidden) as refused:
			driver()

		assert type(refused.value) is subroutine.errors.Forbidden, act
		assert refused.value.detail.startswith("'dozer' is an agent, and "), refused.value.detail
		assert (
			f"{subroutine.permissions.PERSON_ACTS[act]} is a person's act" in refused.value.detail
		), refused.value.detail

	assert cast.carrie_anne.is_active
	assert cast.tank.responsible_user_id == cast.dozer.id


def test_a_person_is_never_refused_by_the_guard (session: sqlalchemy.orm.Session) -> None:
	"""The other direction: the guard asks nothing of a person, so whatever refuses them is elsewhere."""

	cast = _cast(session)

	for act in subroutine.permissions.PERSON_ACTS:
		subroutine.domain.accountability.refuse_a_person_act(
			session, act, by=cast.laurence, on=cast.carrie_anne
		)
		subroutine.domain.accountability.refuse_a_person_act(session, act, by=None)


def test_it_is_asked_before_anything_else (session: sqlalchemy.orm.Session) -> None:
	"""First, before a read-only session or the agent tools' ceiling would answer otherwise."""

	cast = _cast(session)
	narrowed = dataclasses.replace(
		Principal(user=cast.dozer), read_only=True, through_the_agent_tools=True
	)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.authentication.issue_token(
			session, user=cast.carrie_anne, title="As them", actor=narrowed
		)

	assert "is a person's act" in refused.value.detail, refused.value.detail


def test_an_agent_acts_on_its_own_agents (session: sqlalchemy.orm.Session) -> None:
	"""Within its own subtree the guard lets it through, and its permissions decide as anybody's do.

	An agent making a sub-agent and its credential, and retiring one, is what ``agent create``
	relies on, which decision `#4235` kept; and its refusal names the account outside it.
	"""

	cast = _cast(session)
	as_dozer = Principal(user=cast.dozer)

	row, _issued = subroutine.domain.authentication.issue_token(
		session, user=cast.tank, title="Its own agent's", actor=as_dozer
	)
	subroutine.domain.tokens.revoke(session, row, actor=as_dozer)
	subroutine.domain.users.set_active(session, cast.tank, active=False, actor=as_dozer)

	assert row.revoked_at is not None
	assert not cast.tank.is_active

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.authentication.issue_token(
			session, user=cast.laurence, title="Its person's", actor=as_dozer
		)

	assert "unless that account answers to it, as 'laurence' does not" in refused.value.detail
	assert refused.value.hint == "Ask 'laurence', who answers for 'dozer'."


#: Every function in the domain that reads whether an account is an agent, and why that does not
#: decide what it may do. **Deleting an entry is what closes it**, and a new one is a decision: the
#: rule (decision `#4515`) is that a person's acts are refused in one place, and a second place
#: branching on an agent is how seven of them came to disagree.
NOT_DECIDING: dict[str, str] = {
	"accountability.py:chain": (
		"Walks the chain of responsibility, which an agent has and a person ends - what the "
		"guard and every door ask of, not a decision about one act. `#473`."
	),
	"accountability.py:answers_to": (
		"A person answers to nobody, so the walk is skipped; the answerer's stop asks it "
		"(decision `#4516`)."
	),
	"accountability.py:refuse_a_person_act": "The guard itself (`SR#4565`, decision `#4515`).",
	"accountability.py:agents_answering_to": (
		"Lists the agents beneath an account, which is what a departure stops and what the "
		"answerer may act on. `#479`."
	),
	"accountability.py:levels_below": (
		"Measures the chain below an account before a hand-over, so it cannot run past "
		"`MAX_DEPTH`. `#3939`."
	),
	"accountability.py:answerable_for_many": (
		"Renders who answers for each row, a page at a time. `#1414`."
	),
	"accountability.py:account_parents_for_many": (
		"Renders each account's account parent, a page at a time. `#2789`."
	),
	"authorization.py:reaches": (
		"An agent reaches a workspace only while the person at the top of its chain is a member "
		"there - its standing in that workspace, the same for every act (decision `#4518`)."
	),
	"local.py:_live_users": (
		"Local mode is the sole person when nobody is named, so this counts people - which "
		"account the terminal is, never what it may do (decision `#4514`)."
	),
	"local.py:_count": (
		"The same count, to say whether a sole person exists (decision `#4514`)."
	),
	"schedule.py:zone_set_by": (
		"An agent that names no timezone reads days in the zone of an account above it, so the "
		"walk is for an agent only. `#3154`."
	),
	"sounds.py:_who": "Says *agent* or *person* in the sentence a sound is described by. `#2721`.",
	"tokens.py:_owner_for": (
		"`--service-account` names an agent, so an existing account under that name must be one: "
		"what was asked for, not what the actor may do. `#348`."
	),
	"users.py:transfer": (
		"Only an agent has somebody answering for it, so handing over a person is refused as a "
		"request that means nothing. `#478`."
	),
	"repair.py:repairers": (
		"Who could repair a scope is people, not agents (decision `#4526`): whom a last-one "
		"refusal counts, never what the actor may do."
	),
	"users.py:set_password": "An agent authenticates with a token and has no password. `#487`.",
}


def _deciding (root: pathlib.Path) -> dict[str, list[int]]:
	"""Return every function under ``root`` that reads ``is_service_account``, with the lines.

	Takes the tree as an argument, `#405`'s rule, so a synthetic offender goes through this scan
	rather than through a copy of it.
	"""

	found: dict[str, list[int]] = {}

	def visit (node: ast.AST, path: str, names: tuple[str, ...]) -> None:
		"""Walk one node, remembering the innermost function it is in."""

		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			names = (*names, node.name)

		if (
			isinstance(node, ast.Attribute)
			and node.attr == "is_service_account"
			and isinstance(node.ctx, ast.Load)
		):
			found.setdefault(f"{path}:{names[-1] if names else '<module>'}", []).append(
				node.lineno
			)

		for child in ast.iter_child_nodes(node):
			visit(child, path, names)

	for path in sorted(root.rglob("*.py")):
		visit(ast.parse(path.read_text(encoding="utf-8")), path.relative_to(root).as_posix(), ())

	return found


def test_nothing_outside_the_guard_decides_by_an_account_being_an_agent () -> None:
	"""A read of ``is_service_account`` the register does not explain is a person's act coming back."""

	unexplained = sorted(set(_deciding(DOMAIN)) - set(NOT_DECIDING))

	assert not unexplained, (
		f"{unexplained} read whether an account is an agent. A person's act is refused by "
		f"accountability.refuse_a_person_act and nowhere else (decision `#4515`): add the act to "
		f"permissions.PERSON_ACTS, or say in NOT_DECIDING why this read decides nothing."
	)


def test_the_register_names_only_functions_that_still_read_it () -> None:
	"""An entry whose read has gone is a fossil that reads as a considered decision."""

	gone = sorted(set(NOT_DECIDING) - set(_deciding(DOMAIN)))

	assert not gone, f"{gone} no longer read whether an account is an agent. Delete them."


def test_every_reason_is_a_reason () -> None:
	"""Each entry cites the item, the decision or the section that settles it."""

	for name, reason in NOT_DECIDING.items():
		assert "`#" in reason or "§" in reason, f"NOT_DECIDING[{name!r}] cites nothing"


def test_the_scan_finds_the_check_it_replaced (tmp_path: pathlib.Path) -> None:
	"""Fed the shape the seven inline checks had, through the real scanner, it reports it."""

	(tmp_path / "users.py").write_text(
		"def set_active (actor):\n\tif actor.user.is_service_account:\n\t\traise Forbidden\n",
		encoding="utf-8",
	)

	assert _deciding(tmp_path) == {"users.py:set_active": [2]}
