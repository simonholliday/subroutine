"""``/v1/workspaces`` over HTTP — docs/design.md §8.6.

These endpoints exist because using the product on its own plan showed the gap: a personal
to-do list and a project's backlog shared one instance, ``GET /v1/agenda?workspace_id=`` was
built to separate them, and nothing could create a second workspace to point it at.

The two that matter most are the permission tier — ``instance:workspace_create`` is not
something a task-scoped agent quietly acquires — and the fact that the slug cannot be changed.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation whose founder is a superuser, as ``init`` leaves one."""

	return test_api_tasks._world(session)


def test_a_workspace_can_be_created_and_read_back (world: test_api_tasks.World) -> None:
	"""The whole point: a second workspace, addressable by its short name."""

	response = world.call(
		"POST",
		"/v1/workspaces",
		json={"slug": "acme", "title": "Acme Engineering", "timezone": "Europe/London"},
	)

	assert response.status_code == 201

	body = response.json()

	assert body["slug"] == "acme"
	assert body["title"] == "Acme Engineering"
	assert body["timezone"] == "Europe/London"
	assert body["version"] == 1

	# Addressed by short name, not only by id — §13.7 puts the slug in every address.
	by_slug = world.call("GET", "/v1/workspaces/acme")

	assert by_slug.status_code == 200
	assert by_slug.json()["id"] == body["id"]

	by_id = world.call("GET", f"/v1/workspaces/{body['id']}")

	assert by_id.status_code == 200
	assert by_id.json()["slug"] == "acme"


def test_a_new_workspace_is_stocked_and_owned (world: test_api_tasks.World) -> None:
	"""A workspace with no vocabulary cannot hold a task, and one with no owner cannot be run.

	So creating one has to do both, in the same transaction. Proved by using it rather than by
	counting rows: a project and a task land in it immediately afterwards.
	"""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	project = world.call(
		"POST", "/v1/projects", json={"key": "web", "title": "Web", "workspace_id": "acme"}
	)

	assert project.status_code == 201

	task = world.call(
		"POST",
		"/v1/tasks",
		json={"text": "Something there", "project": "web", "workspace_id": "acme"},
	)

	assert task.status_code == 201
	assert task.json()["project_key"] == "web"

	# And the ref sequence is the new workspace's own, not a continuation of the founder's.
	assert task.json()["ref"] == 1


def test_the_new_workspace_appears_in_the_listing_and_in_me (
	world: test_api_tasks.World,
) -> None:
	"""``/v1/me`` and ``/v1/workspaces`` must agree, since both go through ``readable``."""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	listed = world.call("GET", "/v1/workspaces")

	assert listed.status_code == 200

	slugs = [item["slug"] for item in listed.json()["items"]]

	assert "acme" in slugs

	from_me = [item["slug"] for item in world.call("GET", "/v1/me").json()["workspaces"]]

	assert sorted(slugs) == sorted(from_me)


def test_the_agenda_can_now_narrow_to_one_of_two_workspaces (
	world: test_api_tasks.World,
) -> None:
	"""The reason these endpoints exist, end to end.

	This is the case that could not be *built* before: two workspaces, and an agenda asked about
	one of them. Previously the filter had exactly one legal value.
	"""

	world.call("POST", "/v1/tasks", json={"text": "Buy salad"})
	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})
	world.call(
		"POST", "/v1/projects", json={"key": "web", "title": "Web", "workspace_id": "acme"}
	)
	world.call(
		"POST",
		"/v1/tasks",
		json={"text": "Ship the release", "project": "web", "workspace_id": "acme"},
	)

	everywhere = world.call("GET", "/v1/agenda").json()
	titles = {task["title"] for task in everywhere["unscheduled"]}

	assert titles == {"Buy salad", "Ship the release"}

	narrowed = world.call("GET", "/v1/agenda?workspace_id=acme").json()

	assert {task["title"] for task in narrowed["unscheduled"]} == {"Ship the release"}
	assert narrowed["unscheduled_total"] == 1


def test_the_title_the_timezone_and_the_slug_can_all_change (
	world: test_api_tasks.World,
) -> None:
	"""§8.3 semantics, including the field that used not to be offered.

	**The slug was refused here until `#295`**, on the stated grounds that it lives "in other
	people's notes, in shell history and in ``config.toml`` on other machines". No connection
	and no setting names a workspace, so the last of those was false — and what remained was
	the exposure a project key has, which `#176` had already decided is acceptable when the
	caller is told what stops working first.
	"""

	created = world.call(
		"POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"}
	).json()

	changed = world.call(
		"PATCH", "/v1/workspaces/acme", json={"title": "Acme Ltd", "timezone": "Asia/Tokyo"}
	)

	assert changed.status_code == 200
	assert changed.json()["title"] == "Acme Ltd"
	assert changed.json()["timezone"] == "Asia/Tokyo"
	assert changed.json()["version"] == created["version"] + 1

	# Omitted is untouched; null clears (§8.3). A cleared zone means "not stated", so the
	# instance's own shows through rather than being replaced by a helpful default.
	kept = world.call("PATCH", "/v1/workspaces/acme", json={"description": "The one"})

	assert kept.json()["timezone"] == "Asia/Tokyo"

	cleared = world.call("PATCH", "/v1/workspaces/acme", json={"timezone": None})

	assert cleared.json()["timezone"] is None

	# The slug moves, and the workspace is the same row — so it answers at the new address
	# and not at the old one. There is deliberately no alias: retiring a name retires it.
	renamed = world.call("PATCH", "/v1/workspaces/acme", json={"slug": "acme-two"})

	assert renamed.status_code == 200
	assert renamed.json()["slug"] == "acme-two"
	assert world.call("GET", "/v1/workspaces/acme-two").status_code == 200
	assert world.call("GET", "/v1/workspaces/acme").status_code == 404

	# **Validated exactly as creation validates one**, so a rename cannot arrive at a name
	# nobody could have chosen — one validator, or the two paths drift.
	for refused in ("2026", "all", ""):
		answer = world.call("PATCH", "/v1/workspaces/acme-two", json={"slug": refused})

		assert answer.status_code in (409, 422), f"{refused!r} was accepted"


def test_a_bad_timezone_is_refused_when_written_not_when_read (
	world: test_api_tasks.World,
) -> None:
	"""**Found while building this.** ``workspaces.create`` never validated its timezone.

	An unknown zone was stored happily and then failed on the next date computation, with a 422
	naming the *request's* timezone — a message about the wrong thing, arriving long after the
	mistake. Anything a client can send is checked in the service layer, where the message can
	name the field.
	"""

	refused = world.call(
		"POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme", "timezone": "Mars/Olympus"}
	)

	assert refused.status_code == 422
	assert "timezone" in refused.text

	# And nothing was created by the attempt.
	assert world.call("GET", "/v1/workspaces/acme").status_code == 404

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	assert (
		world.call("PATCH", "/v1/workspaces/acme", json={"timezone": "Mars/Olympus"}).status_code
		== 422
	)
	assert world.call("GET", "/v1/workspaces/acme").json()["timezone"] == "UTC"


def test_a_narrowed_token_cannot_create_a_workspace (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``instance:workspace_create`` is an instance verb and no role carries it (§7.1).

	The recurring defect here is a rule documented, believed and enforced nowhere, and it has
	twice been a privilege escalation. Creating a workspace mints a whole tenancy with its own
	ref sequence and vocabulary; a ``task:read`` agent must not be able to.
	"""

	narrowed = test_api_tasks._world(session, scopes=["task:read"])

	assert (
		narrowed.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"}).status_code
		== 403
	)


def test_a_workspace_you_cannot_reach_reads_as_absent (
	world: test_api_tasks.World,
) -> None:
	"""§7.3a: "forbidden" would confirm it exists. Absent is the honest answer.

	The outsider is a *separate account*, not a second call to the world fixture:
	``bootstrap.initialise`` is idempotent by the instance row, so calling it twice hands back
	the same user with a different token — an "outsider" who is in fact the owner, and a test
	that would have passed while proving nothing.
	"""

	world.call("POST", "/v1/workspaces", json={"slug": "secret", "title": "Secret"})

	stranger = subroutine.domain.users.create(
		session=world.session, username=f"other-{uuid.uuid4().hex[:8]}"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session=world.session, user=stranger, title="Outsider"
	)
	world.session.flush()

	secret = issued.value.get_secret_value()
	headers = {"authorization": f"Bearer {secret}"}

	assert api_support.call(
		world.application, "GET", "/v1/workspaces/secret", headers=headers
	).status_code == 404
	assert api_support.call(
		world.application,
		"PATCH",
		"/v1/workspaces/secret",
		headers=headers,
		json={"title": "Mine now"},
	).status_code == 404
	assert "secret" not in api_support.call(
		world.application, "GET", "/v1/workspaces", headers=headers
	).text


def test_a_duplicate_short_name_is_refused (world: test_api_tasks.World) -> None:
	"""A slug is an address, so two workspaces cannot share one."""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})
	again = world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme Two"})

	assert again.status_code == 409
	assert again.json()["code"] == "duplicate_key"


@pytest.mark.parametrize(
	"slug",
	[
		"2026",  # would read as a number in `work/2026/#42`
		"local",  # reserved: it names *this* installation in an address (§13.7)
		"default",  # reserved likewise
		"",
		"---",  # normalises to nothing usable
	],
)
def test_an_unusable_short_name_is_refused (
	world: test_api_tasks.World, slug: str
) -> None:
	"""The slug rules are structural, because a slug is the middle of an address (§13.7)."""

	response = world.call("POST", "/v1/workspaces", json={"slug": slug, "title": "Nope"})

	assert response.status_code in {409, 422}, response.text
	assert "slug" in response.text


def test_a_stale_version_is_refused (world: test_api_tasks.World) -> None:
	"""§8.9, through ``If-Match`` and through the body, and the 409 carries the current row."""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})
	world.call("PATCH", "/v1/workspaces/acme", json={"title": "Acme Ltd"})

	stale = world.call(
		"PATCH", "/v1/workspaces/acme", json={"title": "Acme Inc", "expected_version": 1}
	)

	assert stale.status_code == 409
	assert stale.json()["code"] == "version_conflict"

	# The current entity comes back, so a caller can merge without a second request.
	assert stale.json()["current"]["title"] == "Acme Ltd"

	by_header = world.call(
		"PATCH",
		"/v1/workspaces/acme",
		json={"title": "Acme Inc"},
		headers={"If-Match": "1"},
	)

	assert by_header.status_code == 409


def test_the_listing_shapes_like_every_other (world: test_api_tasks.World) -> None:
	"""``?fields=`` and ``?format=`` are read off the view, so they cannot drift (§14.10)."""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	compact = world.call("GET", "/v1/workspaces?format=compact")

	assert compact.status_code == 200
	assert any("acme" in line for line in compact.json()["items"])

	selected = world.call("GET", "/v1/workspaces?fields=slug")

	assert selected.status_code == 200
	assert set(selected.json()["items"][0]) == {"slug"}

	# And a parameter this listing does not declare is refused rather than ignored (§8.1).
	assert world.call("GET", "/v1/workspaces?nonsense=1").status_code == 422


def test_the_service_refuses_an_unknown_actor_free_caller (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``actor=None`` is the internal-caller escape hatch, and it must still validate input.

	``bootstrap`` is the one legitimate ``None`` caller. The permission check is skipped for it;
	nothing else is, which is what stops the escape hatch being a hole.
	"""

	founder = subroutine.domain.workspaces.create(
		session,
		slug=f"ws-{uuid.uuid4().hex[:8]}",
		title="Fine",
		owner=_a_user(session),
	)

	assert founder.timezone == "UTC"

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.workspaces.create(
			session,
			slug=f"ws-{uuid.uuid4().hex[:8]}",
			title="Bad zone",
			owner=_a_user(session),
			timezone="Mars/Olympus",
		)


def _a_user (session: sqlalchemy.orm.Session) -> typing.Any:
	"""Return a user to own a workspace under test."""

	return subroutine.domain.bootstrap.initialise(
		session, username=f"u-{uuid.uuid4().hex[:8]}", instance_name="Test"
	).user


def test_a_workspace_made_over_http_can_be_filed_into (
	world: test_api_tasks.World,
) -> None:
	"""``#301``, at the surface that shipped it.

	``POST /v1/workspaces`` produced a workspace with no Inbox from M1 until 2026-08-02, so
	``POST /v1/tasks`` with no project — the ordinary way to file one — refused every time.
	Nothing caught it because no client could create a workspace until `#300`, and every test
	here named a project immediately afterwards.

	The second call is the whole test. Creating one was never the broken part.
	"""

	created = world.call(
		"POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"}
	)

	assert created.status_code == 201

	filed = world.call(
		"POST", "/v1/tasks", json={"title": "Buy milk", "workspace_id": "acme"}
	)

	assert filed.status_code == 201, filed.text
	assert filed.json()["project_key"] == "inbox"
	assert filed.json()["ref"] == 1


# --------------------------------------------------------------------------------------
# Deleting one, and bringing it back — item `#704`
# --------------------------------------------------------------------------------------


def test_a_workspace_can_be_deleted_and_restored (world: test_api_tasks.World) -> None:
	"""The whole of `#704`: a soft delete with a restore, matching a project's (`#308`).

	Both return the workspace rather than a 204, so a caller can read ``deleted_at`` back and
	knows the address to hand the restore.
	"""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	removed = world.call("DELETE", "/v1/workspaces/acme")

	assert removed.status_code == 200, removed.text
	assert removed.json()["deleted_at"] is not None

	assert world.call("GET", "/v1/workspaces/acme").status_code == 404

	listed = world.call("GET", "/v1/workspaces").json()["items"]

	assert [row["slug"] for row in listed] == [world.workspace.slug]

	back = world.call("POST", "/v1/workspaces/acme/restore")

	assert back.status_code == 200, back.text
	assert back.json()["deleted_at"] is None
	assert world.call("GET", "/v1/workspaces/acme").status_code == 200


def test_what_is_in_a_deleted_workspace_leaves_and_returns_with_it (
	world: test_api_tasks.World,
) -> None:
	"""The claim ``delete``'s docstring makes, driven rather than asserted in prose.

	Nothing cascades, so the way to know the exclusion works is to look for the contents
	through a listing that never mentions a workspace — which is every listing, since they all
	take their ids from ``workspaces.readable``.
	"""

	made = world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"}).json()
	filed = world.call(
		"POST", "/v1/tasks", json={"title": "Acme deliverable", "workspace_id": "acme"}
	)

	assert filed.status_code == 201, filed.text

	def titles () -> list[str]:
		"""Every task this caller can see, across every workspace they can reach."""

		found: list[str] = []

		for row in world.call("GET", "/v1/workspaces").json()["items"]:
			listing = world.call("GET", "/v1/tasks", params={"workspace_id": row["slug"]})
			found.extend(task["title"] for task in listing.json()["items"])

		return found

	assert "Acme deliverable" in titles()

	world.call("DELETE", "/v1/workspaces/acme")

	assert "Acme deliverable" not in titles()

	# And it is not reachable by naming the workspace either, which is the half a listing
	# scoped to the caller's own reach cannot show.
	assert (
		world.call("GET", "/v1/tasks", params={"workspace_id": "acme"}).status_code == 404
	)
	assert world.call("GET", "/v1/tasks", params={"workspace_id": made["id"]}).status_code == 404

	world.call("POST", "/v1/workspaces/acme/restore")

	assert "Acme deliverable" in titles()


def test_the_last_workspace_cannot_be_deleted (world: test_api_tasks.World) -> None:
	"""An installation with none reports itself as interrupted part-way through setup.

	``bootstrap._describe`` raises *this database has an instance identity but no workspace*
	and ``local.current`` tells the reader to run ``init`` — advice that cannot help. So the
	refusal is here rather than in a diagnosis nobody can act on.
	"""

	here = f"/v1/workspaces/{world.workspace.slug}"
	only = world.call("DELETE", here)

	assert only.status_code == 422, only.text
	assert "only workspace" in only.text

	# It becomes deletable the moment there is somewhere else to stand.
	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	assert world.call("DELETE", here).status_code == 200


def test_deleting_frees_the_short_name_and_restoring_says_when_it_is_gone (
	world: test_api_tasks.World,
) -> None:
	"""The direct cost of the unique index being partial, refused by name rather than by 500.

	`#308` met this on project keys. Without the check the restore violates the constraint at
	flush time, which surfaces as an ``IntegrityError`` — a 500 and a bare traceback for an
	ordinary sequence of three requests.
	"""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})
	world.call("DELETE", "/v1/workspaces/acme")

	# The name is free, which is the point of the index being partial.
	again = world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Another Acme"})

	assert again.status_code == 201, again.text

	blocked = world.call("POST", "/v1/workspaces/acme/restore")

	assert blocked.status_code == 409, blocked.text
	assert blocked.json()["code"] == "duplicate_key"
	assert "rename" in blocked.text


def test_restoring_names_the_deleted_one_rather_than_guessing (
	world: test_api_tasks.World,
) -> None:
	"""A slug can name a deleted workspace *and* a live one, and this was measured wrong first.

	Because the index is partial, the sequence *create, rename away, create again, delete,
	rename back* leaves one slug on two rows — the live one older than the deleted one. A
	resolver taking the first match reported **Restored acme** about the workspace that had
	never been deleted, and left the deleted one exactly where it was. ``workspaces.for_restore``
	is why that is not a flag on the ordinary resolver.
	"""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "First"})
	world.call("PATCH", "/v1/workspaces/acme", json={"slug": "elsewhere"})
	second = world.call(
		"POST", "/v1/workspaces", json={"slug": "acme", "title": "Second"}
	).json()
	world.call("DELETE", "/v1/workspaces/acme")
	world.call("PATCH", "/v1/workspaces/elsewhere", json={"slug": "acme"})

	# `acme` now names a live workspace and a deleted one, and the live one is older.
	blocked = world.call("POST", "/v1/workspaces/acme/restore")

	assert blocked.status_code == 409, blocked.text
	assert blocked.json()["code"] == "duplicate_key"

	# The deleted one is still deleted, which is the assertion that fails against the guess.
	assert world.call("GET", "/v1/workspaces/acme").json()["title"] == "First"

	world.call("PATCH", "/v1/workspaces/acme", json={"slug": "elsewhere"})
	back = world.call("POST", f"/v1/workspaces/{second['id']}/restore")

	assert back.status_code == 200, back.text
	assert back.json()["title"] == "Second"


def test_two_deleted_workspaces_sharing_a_name_are_refused_rather_than_guessed (
	world: test_api_tasks.World,
) -> None:
	"""Reachable in four ordinary requests, and an id is the way through.

	``selection._by_name`` refuses two projects sharing a key for the same reason: *there is
	no such workspace* is acted on by checking the spelling, and *there are two* by saying
	which.
	"""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "First"})
	world.call("DELETE", "/v1/workspaces/acme")
	second = world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Second"}).json()
	world.call("DELETE", "/v1/workspaces/acme")

	ambiguous = world.call("POST", "/v1/workspaces/acme/restore")

	assert ambiguous.status_code == 422, ambiguous.text
	assert "More than one deleted workspace" in ambiguous.text

	by_id = world.call("POST", f"/v1/workspaces/{second['id']}/restore")

	assert by_id.status_code == 200, by_id.text
	assert by_id.json()["title"] == "Second"


def test_deleting_a_workspace_needs_the_permission_the_two_roles_differ_by (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#704`'s point: ``workspace:delete`` is the whole of `owner` against `admin`.

	While nothing checked it the two roles were the same role with two descriptions — a
	distinction the product published, seeded, and could not honour.

	Aimed at the narrowed caller's *own* workspace, which it owns and can read, so the refusal
	can only be about the verb. The last-workspace rule would refuse this too and is checked
	after the permission, which is the order that makes this test mean what it says.
	"""

	narrowed = test_api_tasks._world(session, scopes=["workspace:read"])
	refused = narrowed.call("DELETE", f"/v1/workspaces/{narrowed.workspace.slug}")

	assert refused.status_code == 403, refused.text

	# The same request with an unnarrowed credential reaches the *next* check rather than
	# this one — which is what shows the 403 was the scope and not the row.
	full = test_api_tasks._world(session)

	assert full.call("DELETE", f"/v1/workspaces/{full.workspace.slug}").status_code == 422


def test_deleting_and_restoring_are_both_idempotent (world: test_api_tasks.World) -> None:
	"""When something was thrown away is a fact worth not overwriting."""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	assert world.call("DELETE", "/v1/workspaces/acme").status_code == 200

	# **Over HTTP the second one is a 404, matching a project's**: the route resolves among
	# the live workspaces, so from a caller's side the row is simply gone. The idempotence is
	# a property of the service, which the local client and every internal caller reach
	# directly, and it is asserted below rather than inferred from the status code.
	assert world.call("DELETE", "/v1/workspaces/acme").status_code == 404

	row = subroutine.domain.workspaces.readable(
		world.session,
		subroutine.domain.authentication.Principal(user=world.user),
		include_deleted=True,
	)
	acme = next(found for found in row if found.slug == "acme")
	held = acme.deleted_at
	version = acme.version

	subroutine.domain.workspaces.delete(world.session, acme)

	assert acme.deleted_at == held
	assert acme.version == version

	restored = world.call("POST", "/v1/workspaces/acme/restore").json()

	assert world.call("POST", "/v1/workspaces/acme/restore").json()["version"] == (
		restored["version"]
	)


def test_creating_a_workspace_refuses_a_setting_that_changing_one_would_refuse (
	world: test_api_tasks.World,
) -> None:
	"""`SR#1127`. The two ends of one field disagreed, and create is the end reached first.

	``create`` stored ``dict(settings or {})`` while ``update`` ran the registry — so a key
	``PATCH`` refuses by name was accepted silently by the call that *makes* the workspace, and
	the value it refuses was stored. `SR#1025` chose *refuse on write, ignore on read* so a typo
	is caught where somebody can still fix it; creating is a write.

	**Both calls, in one test, because the finding is the disagreement.** Asserting the refusal
	alone would pass on a build where ``PATCH`` had quietly stopped refusing too.
	"""

	made = world.call(
		"POST",
		"/v1/workspaces",
		json={"slug": "acme", "title": "Acme", "settings": {"no_such_setting": 1}},
	)

	assert made.status_code == 422, (
		f"an undeclared setting was accepted at creation: {made.status_code} {made.text[:200]}"
	)
	assert "no_such_setting" in made.text, "the refusal does not name the key"

	# The same body against the other end, which has always refused it — so this test cannot
	# pass by both ends being wrong together.
	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})
	changed = world.call(
		"PATCH", "/v1/workspaces/acme", json={"settings": {"no_such_setting": 1}}
	)

	assert changed.status_code == 422, changed.text


def test_a_workspace_cannot_be_born_naming_a_status_it_does_not_have (
	world: test_api_tasks.World,
) -> None:
	"""The half a registry entry cannot check alone — `SR#1127`.

	``verified`` runs the workspace-aware checks, and on create the vocabulary is seeded in the
	same call, so they are answerable. Skipping them let a workspace be born holding
	``statuses.hidden`` naming statuses that do not exist, which `SR#1029`'s resolution then
	reads out of a blob nothing ever validated.

	**Ordering is the whole fix here**: the shape is checked before the row is stored and the
	references after the seeding, because until the seeding there is nothing to refer to.
	"""

	refused = world.call(
		"POST",
		"/v1/workspaces",
		json={
			"slug": "acme",
			"title": "Acme",
			"settings": {"statuses.hidden": ["no_such_status"]},
		},
	)

	assert refused.status_code == 422, (
		f"a workspace was created hiding a status it has never had: {refused.text[:200]}"
	)

	# **And a real one is accepted**, which is what stops the assertion above being satisfied by
	# refusing every settings map — the failure this fix could most easily have introduced.
	fine = world.call(
		"POST",
		"/v1/workspaces",
		json={"slug": "acme", "title": "Acme", "settings": {"statuses.hidden": ["done"]}},
	)

	assert fine.status_code == 201, fine.text
	assert fine.json()["settings"]["statuses.hidden"] == ["done"]
