"""An account is read within reach - `SR#4945`.

Decision `#4943`, rule 2. **The account directory was the one kind of row with no reach rule**: any
credential, however narrowed, pinned or read-only, was answered with every account on the
installation, and for each who administers the installation, who is active, who answers to whom and
where they are. Section 7 of the design never stated it, so it was an exception nobody had written.

**An account is read by whoever shares a workspace with it, by its own chain** - itself, whoever
it answers to and whatever answers to it - **and by an administrator of accounts.** Naming an
account to act on it is not a read of it. Each clause is pinned here with the accounts it leaves
out, on the listing, on the read of one account, and over HTTP.
"""

import dataclasses
import json
import typing
import uuid

import fastapi
import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.exporting

users = subroutine.domain.users
workspaces = subroutine.domain.workspaces

User = subroutine.db.models.identity.User
Principal = subroutine.domain.authentication.Principal


class Installation:
	"""Two workspaces that share one member, and the accounts in neither.

	* MetaCortex: keanu founded it; carrieanne and hugo are members, and so is claude, an agent
	  that answers to carrieanne.
	* Zion: marcus founded it; tank and hugo are members.
	* In neither: scout, an agent that answers to claude; laurence, who administers the
	  installation; and gloria, who is on it and nowhere.
	"""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make everybody, through the domain as an installation would."""

		self.session = session
		self.suffix = uuid.uuid4().hex[:6]
		self.keanu = self._person("keanu")
		self.carrieanne = self._person("carrieanne")
		self.hugo = self._person("hugo")
		self.marcus = self._person("marcus")
		self.tank = self._person("tank")
		self.gloria = self._person("gloria")
		self.laurence = users.create(
			session, username=f"laurence-{self.suffix}", is_superuser=True
		)
		self.claude = self._agent("claude", self.carrieanne)
		self.scout = self._agent("scout", self.claude)
		self.metacortex = workspaces.create(
			session, slug=f"metacortex-{self.suffix}", title="Metacortex", owner=self.keanu
		)
		self.zion = workspaces.create(
			session, slug=f"zion-{self.suffix}", title="Zion", owner=self.marcus
		)

		for workspace, members in (
			(self.metacortex, (self.carrieanne, self.hugo, self.claude)),
			(self.zion, (self.tank, self.hugo)),
		):
			for member in members:
				workspaces.add_member(session, workspace, member, role_key="member")

		session.flush()

	def _person (self, name: str) -> User:
		"""Make a person with a name nobody else in the run has."""

		return users.create(self.session, username=f"{name}-{self.suffix}")

	def _agent (self, name: str, above: User) -> User:
		"""Make an agent that answers to ``above``."""

		return users.create(
			self.session,
			username=f"{name}-{self.suffix}",
			is_service_account=True,
			responsible_user_id=above.id,
		)

	@property
	def everybody (self) -> set[uuid.UUID]:
		"""Return every account this installation made."""

		return {
			one.id
			for one in (
				self.keanu, self.carrieanne, self.hugo, self.marcus, self.tank, self.gloria,
				self.laurence, self.claude, self.scout,
			)
		}

	def read_by (self, principal: Principal) -> set[uuid.UUID]:
		"""Return which of this installation's accounts the directory answers a principal with."""

		listed = set(
			self.session.scalars(users.readable(self.session, actor=principal).with_only_columns(User.id))
		)

		return listed & self.everybody


def _ids (*accounts: User) -> set[uuid.UUID]:
	"""Return these accounts' ids."""

	return {one.id for one in accounts}


def _presenting (
	session: sqlalchemy.orm.Session, user: User, **narrowing: typing.Any
) -> Principal:
	"""Return the principal an instance builds from a credential issued to ``user``, narrowed so."""

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=user, title=f"narrowed {uuid.uuid4().hex[:6]}", **narrowing
	)
	session.flush()

	return subroutine.domain.authentication.authenticate(
		session, issued.value.get_secret_value(), record_use=False
	)


def test_the_directory_answers_whoever_shares_a_workspace_and_ones_own_chain (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Each clause of the rule, with who it leaves out, for one of everybody."""

	here = Installation(session)
	metacortex = _ids(here.keanu, here.carrieanne, here.hugo, here.claude)
	zion = _ids(here.marcus, here.tank, here.hugo)

	assert here.read_by(Principal(user=here.keanu)) == metacortex, "sharing a workspace"
	assert here.read_by(Principal(user=here.hugo)) == metacortex | zion, "sharing two"
	assert here.read_by(Principal(user=here.marcus)) == zion
	assert here.read_by(Principal(user=here.carrieanne)) == metacortex | _ids(here.scout), (
		"whatever answers to her, through another agent and in no workspace of hers"
	)
	assert here.read_by(Principal(user=here.claude)) == metacortex | _ids(here.scout), (
		"an agent reads where it works, whoever it answers to and whatever answers to it"
	)
	assert here.read_by(Principal(user=here.scout)) == _ids(here.scout, here.claude, here.carrieanne), (
		"an agent in no workspace reads its own chain and nobody else"
	)
	assert here.read_by(Principal(user=here.gloria)) == _ids(here.gloria), "somebody in no workspace"
	assert here.read_by(Principal(user=here.laurence)) == here.everybody, "an administrator of accounts"


def test_a_credential_reads_the_accounts_of_the_workspaces_it_reaches (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A pin, a narrowing to some projects and an administrator's pinned credential each narrow it.

	The one predicate that says which workspaces a credential reaches says whose accounts it
	reads, so the directory cannot answer more than every other listing does.
	"""

	here = Installation(session)
	metacortex = _ids(here.keanu, here.carrieanne, here.hugo, here.claude)
	zion = _ids(here.marcus, here.tank, here.hugo)
	web = subroutine.domain.projects.create(
		session, workspace_id=here.metacortex.id, key="web", title="Website rebuild"
	)

	pinned = _presenting(session, here.hugo, workspace_id=here.zion.id)
	narrowed = _presenting(session, here.hugo, project_scope=[str(web.id)])
	reading = dataclasses.replace(Principal(user=here.hugo), read_only=True)

	assert here.read_by(pinned) == zion, "a credential pinned to one workspace read another's accounts"
	assert here.read_by(narrowed) == metacortex, "one narrowed to a project read past its workspace"
	assert here.read_by(reading) == metacortex | zion, "reading is all a read-only session does"
	assert here.read_by(
		dataclasses.replace(Principal(user=here.laurence), read_only=True)
	) == here.everybody, "an administrator's read-only session read fewer accounts, and reading is all it asks"

	workspaces.add_member(session, here.zion, here.laurence, role_key="member")

	administering = _presenting(session, here.laurence, workspace_id=here.zion.id)

	assert here.read_by(administering) == zion | _ids(here.laurence), (
		"a pinned credential holds nothing over the installation, an administrator's included"
	)


def test_reading_one_account_out_of_reach_is_answered_as_a_name_nobody_has (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The read of one account follows the listing, and says nothing of what it leaves out."""

	here = Installation(session)
	keanu = Principal(user=here.keanu)

	assert users.read(session, here.carrieanne.username, actor=keanu).id == here.carrieanne.id

	with pytest.raises(subroutine.errors.NotFound) as out_of_reach:
		users.read(session, here.tank.username, actor=keanu)

	with pytest.raises(subroutine.errors.NotFound) as nobody:
		users.read(session, "nobody-at-all", actor=keanu)

	assert str(out_of_reach.value).replace(here.tank.username, "X") == str(nobody.value).replace(
		"nobody-at-all", "X"
	), "an account out of reach is told apart from one that does not exist"
	assert out_of_reach.value.hint == nobody.value.hint

	assert users.read(session, here.tank.username, actor=Principal(user=here.laurence)).id == here.tank.id
	assert users.read(session, here.tank.username, actor=None).id == here.tank.id


def test_asking_whose_agents_answer_to_somebody_out_of_reach_is_refused_as_nobody (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``answers_to`` names an account to read by, so it reads that account first."""

	here = Installation(session)

	def answering (principal: Principal, to: User) -> set[uuid.UUID]:
		"""Return the agents the directory says answer to somebody."""

		return set(
			session.scalars(
				users.readable(session, actor=principal, answers_to=to.username).with_only_columns(User.id)
			)
		)

	assert answering(Principal(user=here.carrieanne), here.carrieanne) == _ids(here.claude, here.scout)
	assert answering(Principal(user=here.keanu), here.carrieanne) == _ids(here.claude), (
		"an agent out of the asker's reach was listed because its person is in it"
	)

	with pytest.raises(subroutine.errors.NotFound):
		answering(Principal(user=here.marcus), here.carrieanne)


def test_naming_an_account_to_act_on_it_is_not_a_read_of_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""keanu cannot read tank, and adds him to MetaCortex by name; then he can.

	Over HTTP, since the route is where a name becomes an account: the same name answers 404 to a
	read and is taken by the act.
	"""

	here = Installation(session)
	application: fastapi.FastAPI = api_support.build_app(api_support.factory_for(session))
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=here.keanu, title="keanu's own"
	)
	session.flush()
	headers = {"Authorization": f"Bearer {issued.value.get_secret_value()}"}

	def get (path: str) -> tuple[int, str]:
		"""Read one path as keanu."""

		answer = api_support.call(application, "GET", path, headers=headers)

		return answer.status_code, answer.text

	status, said = get(f"/v1/users/{here.tank.username}")

	assert status == 404, said

	status, said = get("/v1/users?limit=200")

	assert status == 200, said
	assert here.carrieanne.username in said and here.tank.username not in said, said
	assert here.laurence.username not in said, "who administers the installation was listed"

	added = api_support.call(
		application,
		"POST",
		f"/v1/workspaces/{here.metacortex.slug}/members",
		headers=headers,
		json={"username": here.tank.username, "role": "member"},
	)

	assert added.status_code == 201, added.text

	status, said = get(f"/v1/users/{here.tank.username}")

	assert status == 200, said


@pytest.mark.parametrize(
	"body",
	[{}, {"is_active": None}, {"responsible": None}, {"is_active": None, "responsible": None}],
	ids=["empty", "is_active null", "responsible null", "both null"],
)
def test_an_edit_that_asks_no_act_is_answered_as_a_read (
	session: sqlalchemy.orm.Session, body: dict[str, typing.Any]
) -> None:
	"""keanu's `PATCH` of tank asking nothing is the 404 his `GET` is - `SR#5115`.

	H-2 of the cold review of 2026-10-17: the edit resolved its name with `by_username` and answered
	with the row, so an empty body read any account on the installation, the one who administers it
	included, and a 200 against a 404 for a name nobody has said which names were taken.
	"""

	here = Installation(session)
	application: fastapi.FastAPI = api_support.build_app(api_support.factory_for(session))
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=here.keanu, title="keanu's own"
	)
	session.flush()
	headers = {"Authorization": f"Bearer {issued.value.get_secret_value()}"}

	def asked (method: str, name: str) -> tuple[int, str]:
		"""Ask one account by name as keanu, with the body under test for an edit.

		The text comes back with the request's own id taken out, which differs on every answer, and
		the name asked for written as ``X``, so two answers can be compared whole.
		"""

		path = f"/v1/users/{name}"

		if method == "PATCH":
			answer = api_support.call(application, method, path, headers=headers, json=body)

		else:
			answer = api_support.call(application, method, path, headers=headers)

		said = answer.json()
		said.pop("request_id", None)

		return answer.status_code, json.dumps(said, sort_keys=True).replace(name, "X")

	for outside in (here.tank, here.laurence, here.scout):
		read = asked("GET", outside.username)
		edited = asked("PATCH", outside.username)

		assert read[0] == 404, read
		assert edited[0] == 404, f"an edit asking nothing read {outside.username}: {edited[1]}"
		assert edited[1] == read[1], (
			"the edit and the read answered an account out of reach differently"
		)

	assert asked("PATCH", "nobody-at-all") == asked("PATCH", here.tank.username), (
		"an account out of reach is told apart from a name nobody has"
	)

	status, said = asked("PATCH", here.carrieanne.username)

	assert status == 200, said
	assert '"username": "X"' in said, "an account within reach is no longer answered"


def test_an_edit_that_asks_an_act_names_its_account_without_reading_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An act named by the edit is refused by the act, not as a name nobody has - `SR#5115`.

	Decision `#4943` rule 2: naming an account to act on it is not a read of it. keanu may not
	deactivate tank, and is told so; answering 404 there would make every act on an account out of
	reach look like a missing one, which is what reading every body through the reach rule did.
	"""

	here = Installation(session)
	application: fastapi.FastAPI = api_support.build_app(api_support.factory_for(session))
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=here.keanu, title="keanu's own"
	)
	session.flush()

	answer = api_support.call(
		application,
		"PATCH",
		f"/v1/users/{here.tank.username}",
		headers={"Authorization": f"Bearer {issued.value.get_secret_value()}"},
		json={"is_active": False},
	)

	assert answer.status_code == 403, answer.text
	assert here.tank.is_active, "the refused act deactivated the account all the same"


def test_an_export_holds_the_accounts_its_reader_can_read (session: sqlalchemy.orm.Session) -> None:
	"""The export's accounts are the directory's, so it is no way round the reach."""

	here = Installation(session)
	held = set(
		session.scalars(
			subroutine.exporting._users(
				session, Principal(user=here.keanu), here.metacortex.id
			).with_only_columns(User.id)
		)
	)

	assert held & here.everybody == _ids(here.keanu, here.carrieanne, here.hugo, here.claude)
