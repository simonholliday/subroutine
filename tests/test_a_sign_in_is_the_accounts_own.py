"""When an account last signed in is its own and its administrators' - `SR#5111`.

**The review's Open Question 10(b), answered in decision `#5097`**: ``last_login_at`` was on the
account view every co-member reads, which told a colleague how often somebody uses the browser. It
leaves that view; the account reads its own on ``/v1/me``, and an administrator of accounts reads
every account's on ``GET /v1/admin/sign-ins``, refused to anybody else. Measured over HTTP, and the
listing through both clients.
"""

import typing
import uuid

import sqlalchemy.orm

import api_support
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.sessions
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks
import test_transport_equivalence

#: The equivalence suite's pair - one installation, reached locally and over HTTP.
pair = test_transport_equivalence.pair
Pair = test_transport_equivalence.Pair


def _reader (world: test_api_tasks.World, secret: str) -> typing.Callable[[str], typing.Any]:
	"""Return a reader making requests with ``secret``."""

	def read (path: str) -> typing.Any:
		"""Make one request as that credential."""

		return api_support.call(
			world.application, "GET", path, headers={"authorization": f"Bearer {secret}"}
		)

	return read


def _signed_in (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> tuple[subroutine.db.models.identity.User, str]:
	"""Sign the world's person in once, and return a colleague who never has, with a credential."""

	_link, link = subroutine.domain.sessions.mint_link(session, user=world.user)
	subroutine.domain.sessions.redeem(session, link)

	keanu = subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, keanu, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=keanu, title="Keanu's"
	)
	session.flush()

	return keanu, str(issued.value.get_secret_value())


def test_a_colleague_reads_no_sign_in (session: sqlalchemy.orm.Session) -> None:
	"""A co-member's account, read alone, in the directory and among the workspace's members,
	carries no ``last_login_at``."""

	world = test_api_tasks._world(session)
	_keanu, secret = _signed_in(session, world)
	read = _reader(world, secret)

	one = read(f"/v1/users/{world.user.username}")

	assert one.status_code == 200, one.text
	assert "last_login_at" not in one.json()

	listed = read("/v1/users").json()["items"]

	assert world.user.username in {row["username"] for row in listed}
	assert [row for row in listed if "last_login_at" in row] == []

	members = read(f"/v1/workspaces/{world.workspace.slug}/members").json()["items"]

	assert world.user.username in {row["user"]["username"] for row in members}
	assert [row for row in members if "last_login_at" in row["user"]] == []


def test_the_account_reads_its_own_on_me (session: sqlalchemy.orm.Session) -> None:
	"""``/v1/me`` says when the account asking last signed in, and null for one that never has."""

	world = test_api_tasks._world(session)
	_keanu, secret = _signed_in(session, world)

	mine = _reader(world, world.secret)("/v1/me")

	assert mine.status_code == 200, mine.text
	assert mine.json()["user"]["last_login_at"] is not None
	assert _reader(world, secret)("/v1/me").json()["user"]["last_login_at"] is None


def test_an_administrator_of_accounts_reads_every_sign_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The listing names every account with its last sign-in, and refuses a colleague and an
	administrator's credential narrowed to tasks."""

	world = test_api_tasks._world(session)
	keanu, secret = _signed_in(session, world)

	listed = _reader(world, world.secret)("/v1/admin/sign-ins")

	assert listed.status_code == 200, listed.text

	seen = {row["username"]: row["last_login_at"] for row in listed.json()["items"]}

	assert seen[world.user.username] is not None
	assert seen[keanu.username] is None
	assert _reader(world, secret)("/v1/admin/sign-ins").status_code == 403

	_row, narrowed = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="Tasks only", scopes=["task:read"]
	)
	session.flush()
	tasks = _reader(world, str(narrowed.value.get_secret_value()))

	assert tasks("/v1/admin/sign-ins").status_code == 403


def test_the_listing_pages_in_the_directory_s_order (session: sqlalchemy.orm.Session) -> None:
	"""A page of one says there is more and its cursor reaches the rest, in the directory's order;
	the directory's own cursor is not one this listing takes."""

	world = test_api_tasks._world(session)
	_signed_in(session, world)
	read = _reader(world, world.secret)

	everyone = [row["username"] for row in read("/v1/admin/sign-ins").json()["items"]]
	walked: list[str] = []
	path = "/v1/admin/sign-ins?limit=1"

	# **Bounded**, so a page that never moves on fails rather than hanging the suite.
	for _ in range(len(everyone) + 1):
		page = read(path).json()
		walked.extend(row["username"] for row in page["items"])

		if not page["page"]["has_more"]:
			break

		path = f"/v1/admin/sign-ins?limit=1&cursor={page['page']['next_cursor']}"

	assert len(everyone) > 1
	assert walked == everyone
	assert walked == [row["username"] for row in read("/v1/users").json()["items"]]

	directory = read("/v1/users?limit=1").json()["page"]["next_cursor"]

	assert read(f"/v1/admin/sign-ins?limit=1&cursor={directory}").status_code != 200


def test_both_clients_list_the_same_sign_ins (pair: Pair) -> None:
	"""The local client and the HTTP client answer the listing alike."""

	_link, link = subroutine.domain.sessions.mint_link(pair.session, user=pair.user)
	subroutine.domain.sessions.redeem(pair.session, link)

	local = pair.local.sign_ins()
	mine = [one for one in local if one.username == pair.user.username]

	assert len(mine) == 1
	assert mine[0].last_login_at is not None
	assert pair.remote.sign_ins() == local
