"""Nothing is said of an account the caller cannot read - `SR#5103`.

M-8 of the cold review of 2026-10-17 (`#5096`), against decision `#4943` rule 2 and section 7.3's
*no answer and no refusal names what the caller cannot read*. Usernames are not secret (`#161`); an
account's standing, its kind and its chain are what the reach rule withholds. **Refusing a
credential for somebody said each of them** - deactivated, an agent nobody answers for, a person's
account - to a caller in another workspace, who reads none of them. Each is asked the accounts
permission first now, and refused as a plain person is; an administrator of accounts still hears
the reason.
"""

import uuid

import fastapi
import pytest
import sqlalchemy.orm

import api_support
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces

users = subroutine.domain.users
workspaces = subroutine.domain.workspaces

User = subroutine.db.models.identity.User


class Installation:
	"""Two workspaces with nobody in common, and the accounts a refusal described.

	* Zion: marcus founded it, and reads nobody in MetaCortex.
	* MetaCortex: keanu founded it. carrieanne is a member, and so is claude, an agent answering to
	  her; gloria was, and has left; cypher is an agent that is still one, answering to joe, who
	  has left.
	* laurence administers the installation, and reads everybody.
	"""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make everybody, through the domain as an installation would."""

		self.session = session
		self.suffix = uuid.uuid4().hex[:6]
		self.keanu = self._person("keanu")
		self.carrieanne = self._person("carrieanne")
		self.gloria = self._person("gloria")
		self.joe = self._person("joe")
		self.marcus = self._person("marcus")
		self.laurence = users.create(
			session, username=f"laurence-{self.suffix}", is_superuser=True
		)
		self.cypher = self._agent("cypher", self.joe)
		self.claude = self._agent("claude", self.carrieanne)
		self.metacortex = workspaces.create(
			session, slug=f"metacortex-{self.suffix}", title="Metacortex", owner=self.keanu
		)
		self.zion = workspaces.create(
			session, slug=f"zion-{self.suffix}", title="Zion", owner=self.marcus
		)

		for member in (self.carrieanne, self.claude, self.gloria, self.joe, self.cypher):
			workspaces.add_member(session, self.metacortex, member, role_key="member")

		session.flush()
		users.set_active(session, self.gloria, active=False)
		users.set_active(session, self.joe, active=False)
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

	def credential (self, user: User) -> dict[str, str]:
		"""Return the headers presenting a fresh credential of ``user``'s."""

		_row, issued = subroutine.domain.authentication.issue_token(
			self.session, user=user, title=f"{user.username}'s own {uuid.uuid4().hex[:6]}"
		)
		self.session.flush()

		return {"Authorization": f"Bearer {issued.value.get_secret_value()}"}


#: Each credential a refusal described an account for, as the field it was named in and who.
DESCRIBED = [
	pytest.param("username", "gloria", id="a person who has left"),
	pytest.param("username", "cypher", id="an agent nobody answers for, by username"),
	pytest.param("service_account", "cypher", id="an agent nobody answers for, as a machine"),
	pytest.param("service_account", "carrieanne", id="a person, as a machine"),
]


def _asked (
	application: fastapi.FastAPI, headers: dict[str, str], field: str, name: str
) -> tuple[int, dict[str, object]]:
	"""Ask for a credential for ``name`` in ``field``, returning the status and the body."""

	answer = api_support.call(
		application,
		"POST",
		"/v1/tokens",
		headers=headers,
		json={"title": f"for {name} {uuid.uuid4().hex[:6]}", field: name},
	)

	return answer.status_code, answer.json()


@pytest.mark.parametrize(("field", "who"), DESCRIBED)
def test_a_credential_refused_outside_reach_is_refused_as_for_anybody (
	session: sqlalchemy.orm.Session, field: str, who: str
) -> None:
	"""marcus is refused a credential for each of them exactly as for carrieanne, an active person.

	**The plain refusal is the control**: what a caller who may not issue for others hears about an
	ordinary account. Answered so for an account in each described state, nothing about the state
	reaches him.
	"""

	here = Installation(session)
	application = api_support.build_app(api_support.factory_for(session))
	headers = here.credential(here.marcus)
	named = getattr(here, who).username

	plain_status, plain = _asked(application, headers, "username", here.carrieanne.username)
	status, said = _asked(application, headers, field, named)

	assert plain_status == 403, plain
	assert (status, said.get("detail"), said.get("errors")) == (
		plain_status, plain.get("detail"), plain.get("errors")
	), said

	for told in ("deactivated", "agent", "person", "answers"):
		assert told not in str(said), f"{told!r} was said of {named}: {said}"


@pytest.mark.parametrize(
	("field", "who", "status", "reason"),
	[
		pytest.param("username", "gloria", 404, "is deactivated", id="a person who has left"),
		pytest.param(
			"username", "cypher", 404, "Nobody who can act answers for", id="an agent nobody answers for"
		),
		pytest.param(
			"service_account", "carrieanne", 422, "is a person's account", id="a person, as a machine"
		),
	],
)
def test_an_administrator_of_accounts_still_hears_the_reason (
	session: sqlalchemy.orm.Session, field: str, who: str, status: int, reason: str
) -> None:
	"""laurence reads every account and may issue for it, so the refusal still says why."""

	here = Installation(session)
	application = api_support.build_app(api_support.factory_for(session))
	answered, said = _asked(application, here.credential(here.laurence), field, getattr(here, who).username)

	assert answered == status, said
	assert reason in str(said.get("detail")), said


def test_a_name_nobody_has_and_ones_own_are_answered_as_before (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A name nobody has is still not found to marcus, and a credential of his own is still his."""

	here = Installation(session)
	application = api_support.build_app(api_support.factory_for(session))
	headers = here.credential(here.marcus)

	status, said = _asked(application, headers, "username", f"nobody-{here.suffix}")

	assert status == 404, said

	status, said = _asked(application, headers, "username", here.marcus.username)

	assert status == 201, said
