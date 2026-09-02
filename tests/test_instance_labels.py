"""What an installation is called, and where it says it is — item `#1669`.

Both are declared editable and neither could be edited by anything. The model's own comment
said *"Editable — it is a label, not an identity"* while ``domain/instances.py`` said *"one
row, written once, never replaced"* — and that second argument is about the **id**, which is
what an agent keys its caches on. Applied to the whole row it made a self-hosted installation
keep the machine's hostname for ever, since ``establish`` returns an existing row untouched
and a second ``init`` therefore changes nothing.

**Every test here reads the value back from somewhere other than the response.** A route that
answers ``200`` without writing is the exact shape this item was filed against, and asserting
on what the handler returned cannot tell the two apart.
"""

import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.instances
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks


def _stored (session: sqlalchemy.orm.Session) -> tuple[str, str]:
	"""Return the name and timezone as the database holds them, not as a caller was told."""

	session.expire_all()
	row = subroutine.domain.instances.require(session)

	return row.name, row.timezone


def test_an_installation_can_be_renamed (session: sqlalchemy.orm.Session) -> None:
	"""The defect itself. Set a name, change it, read it back — which is what `#1669` asks for."""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="First Name"
	)

	assert _stored(session)[0] == "First Name"

	subroutine.domain.instances.update(session, name="Second Name")

	assert _stored(session)[0] == "Second Name"


def test_a_second_init_still_changes_nothing (session: sqlalchemy.orm.Session) -> None:
	"""And that is why an update verb was needed rather than a fix to ``establish``.

	Re-running ``init`` against an existing database has to be safe — it is the common case in
	a container that restarts — so the way to change a name could never be to run it again.
	"""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="First Name"
	)

	instance, created = subroutine.domain.instances.establish(session, name="Second Name")

	assert created is False
	assert instance.name == "First Name"


def test_the_timezone_moves_with_it (session: sqlalchemy.orm.Session) -> None:
	"""The other half, and the one that changes what a day means for everybody."""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test", timezone="UTC"
	)

	subroutine.domain.instances.update(session, timezone="Europe/London")

	assert _stored(session)[1] == "Europe/London"


def test_a_field_that_was_not_asked_for_is_left_alone (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§8.3's distinction, and the reason this reads ``model_fields_set`` rather than the value.

	Reading ``body.name is None`` as *clear it* would mean every request that changed only the
	timezone also tried to blank the name — which is ``Move.parent``'s shipped defect, one
	endpoint along.
	"""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Kept", timezone="UTC"
	)

	subroutine.domain.instances.update(session, timezone="Europe/London")

	assert _stored(session) == ("Kept", "Europe/London")


def test_an_installation_cannot_be_left_without_a_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Refused rather than silently kept: a caller that sent a name meant to change it.

	Reporting the old one back would read as success, which is the failure this whole item is
	about wearing the opposite face.
	"""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Kept"
	)

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.instances.update(session, name="   ")

	assert "needs a name" in str(refusal.value)
	assert _stored(session)[0] == "Kept"


def test_a_zone_the_system_does_not_know_is_refused_in_the_usual_words (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Through ``dates.zone``, so this refuses in the same words a workspace and a user do.

	A private copy of the check here would be a third chance for the three to disagree about
	what an unknown zone does.
	"""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test", timezone="UTC"
	)

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.instances.update(session, timezone="Mars/Olympus_Mons")

	assert _stored(session)[1] == "UTC"


def test_changing_it_needs_permission_over_the_installation (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``instance:admin``, which no role carries and only a superuser holds.

	Deciding what a whole installation is called is not something a member of one workspace in
	it should be able to do — and the timezone half reaches everybody who has not set their own.
	"""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	ordinary = subroutine.domain.users.create(session, username=f"jo-{uuid.uuid4().hex[:8]}")
	session.flush()

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.instances.update(
			session,
			name="Theirs",
			actor=subroutine.domain.authentication.Principal(user=ordinary),
		)

	assert _stored(session)[0] == "Test"


def test_it_can_be_changed_over_http_and_meta_reports_the_change (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Read back through ``/v1/meta`` rather than from the ``PATCH``'s own answer.

	`#1669` asks for exactly this: a route that returns 200 without writing is the shape to
	distrust, and the response body cannot tell that apart from one that did.
	"""

	world = test_api_tasks._world(session)

	assert world.call("GET", "/v1/meta").json()["instance"]["name"] == "Test"

	changed = world.call(
		"PATCH", "/v1/instance", json={"name": "Hyperfence", "timezone": "Europe/London"}
	)

	assert changed.status_code == 200, changed.text

	reported = world.call("GET", "/v1/meta").json()["instance"]

	assert reported["name"] == "Hyperfence"
	assert reported["timezone"] == "Europe/London"
	assert _stored(session) == ("Hyperfence", "Europe/London")


def test_a_request_naming_one_field_over_http_leaves_the_other_alone (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§8.3 at the layer where the analogous defect actually shipped.

	``Move.parent`` read its value rather than ``model_fields_set``, so an omitted parent and an
	explicit null meant the same thing and ``POST /v1/projects/web/move {}`` flattened a whole
	subtree. Here the same mistake would make every timezone change try to blank the name.
	"""

	world = test_api_tasks._world(session)

	world.call("PATCH", "/v1/instance", json={"name": "Hyperfence"})
	world.call("PATCH", "/v1/instance", json={"timezone": "Europe/London"})

	assert _stored(session) == ("Hyperfence", "Europe/London")


def test_the_identity_is_untouched_by_a_rename (session: sqlalchemy.orm.Session) -> None:
	"""The whole reason the immutability argument existed, kept where it belongs.

	An agent keys its caches on the id, notices one instance configured twice under two names
	by it, and labels merged results with it. A name that moves must not move that.
	"""

	world = test_api_tasks._world(session)
	before = world.call("GET", "/v1/meta").json()["instance"]["id"]

	world.call("PATCH", "/v1/instance", json={"name": "Renamed"})

	assert world.call("GET", "/v1/meta").json()["instance"]["id"] == before


def test_somebody_without_the_permission_is_refused_over_http (
	session: sqlalchemy.orm.Session,
) -> None:
	"""And is refused rather than ignored, which a settings write that no-ops would be."""

	world = test_api_tasks._world(session)
	ordinary = subroutine.domain.users.create(session, username=f"jo-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(
		session, world.workspace, ordinary, role_key="member"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=ordinary, title="ordinary"
	)
	session.flush()

	theirs = world._replace(secret=issued.value.get_secret_value())
	refused = theirs.call("PATCH", "/v1/instance", json={"name": "Theirs"})

	assert refused.status_code == 403
	assert _stored(session)[0] == "Test"
