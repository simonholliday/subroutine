"""One ref space serves two kinds, so a refusal about a ref says which it found — `#488`.

``workspace.next_ref_number`` is shared by tasks and documents (§6.2), so ``#480`` may be
either and a caller holding a number does not know which. Until 2026-08-04 both endpoints
answered a ref of the wrong kind by denying it existed:

    GET /v1/tasks/480      -> "There is no task '480' here."
    GET /v1/documents/12   -> "There is no document '12' here."

**§12.2c settled this for the command line and the API never inherited it.** ``subroutine done
4`` used to answer "there is no task #4" about a document printed directly above it, and
``cli/personal._locate(kinds=…)`` was written so the refusal could name the kind. Nothing
carried that to HTTP, so it was still true for every remote caller and for the whole agent
surface — where it is worse, because an agent has no listing in front of it to contradict the
answer.

**It cost a real behaviour change.** `#293`'s reporter met this refusal trying to revise a
conclusion, concluded *"documents look immutable through these tools"*, and stopped filing
documents at all — giving one-item-in-one-place, which is this project's own principle, as the
reason. The workaround was invisible as a workaround.

**Both directions in one file on purpose.** They are one rule with two mirror instances, and
mirror instances are what this codebase drifts apart: the document half already *hedged* that
"a ref that exists may name a task instead", which is what a refusal offers when it has not
looked. Keeping the pair in one place is what stops one half being improved alone.
"""

import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def test_a_documents_ref_is_named_as_a_document (world: test_api_tasks.World) -> None:
	"""The refusal `#293`'s reporter met, now answering the question it was actually asked."""

	document = world.call(
		"POST", "/v1/documents", json={"title": "Why we chose the queue"}
	).json()

	answer = world.call("GET", f"/v1/tasks/{document['ref']}")

	assert answer.status_code == 404

	body = answer.json()

	assert "is a document, not a task" in body["detail"]
	assert "Why we chose the queue" in body["detail"], (
		"the title is what tells a caller whether this is the item they meant"
	)
	assert f"/v1/documents/{document['ref']}" in body["errors"][0]["hint"], (
		"a refusal that names the kind and not the route leaves the caller to guess it"
	)


def test_a_tasks_ref_is_named_as_a_task (world: test_api_tasks.World) -> None:
	"""The mirror, which had a hedge where this now has a fact."""

	task = world.call("POST", "/v1/tasks", json={"title": "Ship the release"}).json()

	answer = world.call("GET", f"/v1/documents/{task['ref']}")

	assert answer.status_code == 404

	body = answer.json()

	assert "is a task, not a document" in body["detail"]
	assert "Ship the release" in body["detail"]
	assert f"/v1/tasks/{task['ref']}" in body["errors"][0]["hint"]


def test_a_ref_naming_nothing_still_reads_as_nothing (world: test_api_tasks.World) -> None:
	"""The other side, so the rule is not "assume the caller asked for the wrong kind".

	Falsifies from the direction a fix is likeliest to be wrong in: a lookup that returned
	something for every ref would satisfy both tests above and be nonsense here.
	"""

	answer = world.call("GET", "/v1/tasks/9999")

	assert answer.status_code == 404
	assert "There is no task" in answer.json()["detail"]

	other = world.call("GET", "/v1/documents/9999")

	assert other.status_code == 404
	assert "There is no document" in other.json()["detail"]


def test_a_change_is_refused_the_same_way_a_read_is (world: test_api_tasks.World) -> None:
	"""The verb an agent actually arrives on, which is what made `#293` a belief.

	Reading a document through the wrong endpoint is a curiosity; *revising* one is the act
	that got refused, and a caller told "there is no task 480" about a `PATCH` concludes the
	item is gone rather than that it asked the wrong endpoint.
	"""

	document = world.call("POST", "/v1/documents", json={"title": "A conclusion"}).json()

	answer = world.call(
		"PATCH", f"/v1/tasks/{document['ref']}", json={"title": "A better conclusion"}
	)

	assert answer.status_code == 404
	assert "is a document, not a task" in answer.json()["detail"]


def test_it_never_names_something_the_caller_cannot_see (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A helpful refusal must not become a way of asking what a private project holds.

	§7.3a: a task in a private project is reported absent rather than forbidden, because
	"forbidden" confirms it exists. A refusal naming the document at a ref would hand back the
	same confirmation — and its *title* with it — which is a worse leak than the one the rule
	exists to prevent. Searched through the caller's own scoping for exactly this reason.
	"""

	world = test_api_tasks._world(session)

	world.call(
		"POST", "/v1/projects", json={"key": "secret", "title": "Secret", "visibility": "private"}
	)
	hidden = world.call(
		"POST", "/v1/documents", json={"title": "The acquisition plan", "project": "secret"}
	).json()

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())
	answer = nosy.call("GET", f"/v1/tasks/{hidden['ref']}")

	assert answer.status_code == 404

	body = answer.json()

	assert "is a document" not in body["detail"], (
		"naming the kind tells an outsider that this ref is taken, and by what"
	)
	assert "acquisition" not in str(body).lower(), "and the title would be the real leak"


@pytest.mark.parametrize("transport", ["local", "remote"])
def test_both_transports_answer_the_same_way (
	session: sqlalchemy.orm.Session, transport: str
) -> None:
	"""`#488`'s third site. The local client kept its own refusal and its own claim about it.

	``clients/local._require``'s docstring said it refused *"the way the API does"* — true when
	written, false the moment the API learned to name the kind, and asserted by nothing. That is
	prose standing in for a guard, which is the failure this project has recorded three times.

	It matters most where it is least visible: a standalone SQLite install goes through the
	local client and never touches the API, and that is the zero-configuration machine an agent
	meets on first contact — the audience `#424`-`#429` came from.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Theirs"
	)
	session.flush()

	factory = api_support.factory_for(session)
	secret = issued.value.get_secret_value()
	client: subroutine.clients.base.Client

	if transport == "local":
		client = subroutine.clients.local.Client(
			subroutine.connections.Connection(name="local"),
			subroutine.config.Settings(dev_mode=True),
			session_factory=factory,
			token=secret,
		)

	else:
		client = subroutine.clients.http.Client(
			subroutine.connections.Connection(name="work", url="https://example.com"),
			token=secret,
			transport=api_support.SyncTransport(api_support.build_app(factory)),
			base_url=api_support.BASE_URL,
		)

	with client:
		written = client.create_document(title="Why we chose the queue")

		with pytest.raises(subroutine.errors.NotFound) as refused:
			client.update(ref=written.ref, title="Something else")

	assert "is a document, not a task" in str(refused.value), (
		f"the {transport} transport still denies the item exists"
	)
	assert "Why we chose the queue" in str(refused.value)
