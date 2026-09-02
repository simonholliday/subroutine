"""Who can find out that a workspace exists here — item `#1418`.

**Two accounts are the whole of why this survived eighteen days.** On an installation with one
superuser, *what I can reach* and *what exists* are the same set, so nothing could disagree.
With two, one a member of a workspace and one not, they part — and they had parted silently in
opposite directions on two surfaces: ``workspaces.readable`` said membership is reach, while
``cli/personal._role``'s docstring said a superuser reaches everything and cited §7.1 for it.
§7.1 said neither, so both had guessed.

**The rule taken (`#1418`, 2026-09-02): membership is reach, and discovery is separate.**
``readable`` is unchanged, so nothing a workspace *contains* widened. What an administrator
gains is the ability to find out a workspace is there and to name it in order to join — and
joining writes a membership row, which is an event with a date and an actor.

Every test here is written so that it would pass on a one-account installation for the wrong
reason if it could, and none can: each names two principals explicitly.
"""

import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.projects
import subroutine.domain.scoping
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks


def _a_second_superuser (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> tuple[subroutine.db.models.identity.User, test_api_tasks.World]:
	"""Add another superuser who is a member of nothing, and a caller for them."""

	account = subroutine.domain.users.create(
		session, username=f"other-{uuid.uuid4().hex[:8]}", is_superuser=True
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=account, title="the other superuser"
	)
	session.flush()

	return account, world._replace(secret=issued.value.get_secret_value())


def test_a_superuser_who_is_not_a_member_still_cannot_reach_a_workspace (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The rule, stated where nothing stated it: membership is reach, superuser or not.

	This is the half `cli/personal._role` claimed was false. It is what makes the whole
	widening below safe, so it is asserted first and directly rather than inferred.
	"""

	world = test_api_tasks._world(session)
	other, _theirs = _a_second_superuser(session, world)

	reachable = subroutine.domain.workspaces.readable(
		session, subroutine.domain.authentication.Principal(user=other)
	)

	assert world.workspace.id not in {row.id for row in reachable}


def test_an_administrator_can_find_out_a_workspace_exists (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The defect. Before this, a workspace somebody was not in was indistinguishable from one
	that did not exist — and the failure was an *empty list*, which reads as nothing being
	there rather than as something unseen.
	"""

	world = test_api_tasks._world(session)
	_other, theirs = _a_second_superuser(session, world)

	reachable = theirs.call("GET", "/v1/workspaces").json()["items"]

	assert world.workspace.slug not in {row["slug"] for row in reachable}, (
		"the ordinary listing must not widen"
	)

	found = theirs.call("GET", "/v1/instance/workspaces").json()["items"]
	named = {row["slug"]: row for row in found}

	assert world.workspace.slug in named
	assert named[world.workspace.slug]["joined"] is False
	assert named[world.workspace.slug]["members"] == 1


def test_the_listing_says_which_ones_the_reader_is_inside (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``joined`` is the field that makes the answer actionable rather than merely complete."""

	world = test_api_tasks._world(session)

	found = world.call("GET", "/v1/instance/workspaces").json()["items"]
	named = {row["slug"]: row for row in found}

	assert named[world.workspace.slug]["joined"] is True


def test_discovery_needs_permission_over_the_installation (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``instance:admin``, which no role carries. A workspace admin is not an instance admin."""

	world = test_api_tasks._world(session)
	ordinary = subroutine.domain.users.create(session, username=f"jo-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(
		session, world.workspace, ordinary, role_key="admin"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=ordinary, title="a workspace admin"
	)
	session.flush()

	theirs = world._replace(secret=issued.value.get_secret_value())

	assert theirs.call("GET", "/v1/instance/workspaces").status_code == 403


def test_an_administrator_can_name_a_workspace_in_order_to_join_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Discovery is useless without this, and it is the half the item did not name.

	Every ``{id_or_slug}`` route resolves through one function that searched only what the
	caller could read — so an administrator could not list a workspace's members, add
	themselves to it, or delete it, and was told it did not exist.
	"""

	world = test_api_tasks._world(session)
	other, theirs = _a_second_superuser(session, world)

	joined = theirs.call(
		"POST",
		f"/v1/workspaces/{world.workspace.slug}/members",
		json={"username": other.username, "role": "member"},
	)

	assert joined.status_code == 201, joined.text

	reachable = theirs.call("GET", "/v1/workspaces").json()["items"]

	assert world.workspace.slug in {row["slug"] for row in reachable}


def test_naming_a_workspace_is_not_reading_what_is_in_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**The containment that makes the widening safe, asserted rather than argued.**

	``resolve`` widened; ``readable`` did not. Every listing of tasks, documents and projects
	resolves its workspace through ``selection.workspace``, which goes through ``readable`` —
	so an administrator can act on the container and still sees nothing inside it until they
	join. If that ever stops being true, this fails.
	"""

	world = test_api_tasks._world(session)
	world.call("POST", "/v1/tasks", json={"title": "Something private"})
	_other, theirs = _a_second_superuser(session, world)

	# They can name it — that is the widening.
	assert theirs.call(
		"GET", f"/v1/workspaces/{world.workspace.slug}/members"
	).status_code == 200

	# And they cannot see into it.
	refused = theirs.call("GET", f"/v1/tasks?workspace_id={world.workspace.slug}")

	assert refused.status_code == 404, refused.text


def test_somebody_without_the_permission_is_still_told_it_does_not_exist (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§7.3a's concealment, unchanged for the caller it was written for.

	A refusal naming a permission would confirm the workspace is there. The widening is for
	``instance:admin`` and for nobody else, and this is what says so.
	"""

	world = test_api_tasks._world(session)
	stranger = subroutine.domain.users.create(session, username=f"jo-{uuid.uuid4().hex[:8]}")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=stranger, title="a stranger"
	)
	session.flush()

	theirs = world._replace(secret=issued.value.get_secret_value())
	refused = theirs.call("GET", f"/v1/workspaces/{world.workspace.slug}/members")

	assert refused.status_code == 404


def test_a_pinned_credential_cannot_read_past_its_pin (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A credential that said what it was for does not get to be the instance owner asking.

	``readable`` narrows to the pin, and the administrator path deliberately refuses a pinned
	principal rather than searching past one — which keeps the rule that a credential may never
	reach further than it was issued to.
	"""

	world = test_api_tasks._world(session)
	other, _theirs = _a_second_superuser(session, world)

	elsewhere = subroutine.domain.workspaces.create(
		session, slug=f"ws{uuid.uuid4().hex[:6]}", title="Elsewhere", owner=other, actor=None
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=other, title="pinned", workspace_id=elsewhere.id
	)
	session.flush()

	pinned = world._replace(secret=issued.value.get_secret_value())

	assert pinned.call(
		"GET", f"/v1/workspaces/{world.workspace.slug}/members"
	).status_code == 404


def test_joining_a_workspace_is_recorded (session: sqlalchemy.orm.Session) -> None:
	"""What makes this safer than granting ambient reach to everything.

	An administrator entering a workspace they were not in is a dated, attributed act with a
	membership row behind it — where a superuser branch in ``readable`` would have been a
	property of a flag, leaving no trace of the moment it started.
	"""

	world = test_api_tasks._world(session)
	other, theirs = _a_second_superuser(session, world)

	theirs.call(
		"POST",
		f"/v1/workspaces/{world.workspace.slug}/members",
		json={"username": other.username, "role": "member"},
	)

	# **Read from the event table rather than from ``/v1/changes``.** That feed holds recent
	# events back behind a watermark so a sequence reader cannot skip rows, so an event written
	# a moment ago is correctly absent from it — which would make this test measure the
	# watermark rather than the recording.
	model = subroutine.db.models.activity.Event
	recorded = session.scalars(
		sqlalchemy.select(model).where(
			model.entity_type == "workspace_member",
			model.workspace_id == world.workspace.id,
		)
	).all()

	joined_by = {row.actor_user_id for row in recorded}

	assert other.id in joined_by, "joining left no trace of who did it"
