"""Who may hide a project, and what hiding or moving one may leave - `SR#5099` and `SR#5101`.

H-1 and M-4 of the cold review of 2026-10-17, decided in `#5097`. **The stand-in steward is for
private projects**: once a public project's owner could no longer act, any member could make it
private, take it over, or move somebody else's work out of the Inbox into their own private project.
**Making a project private, or moving it under a private one, asks the last-one question**: jo made
``outer`` private, or moved it under her ``vault``, and pat's private project inside it, which pat
saw without being shared into ``outer``, was seen by nobody.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.hierarchy
import subroutine.domain.projects
import subroutine.domain.repair
import subroutine.domain.stewardship
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks
import test_stewardship

projects = subroutine.domain.projects

_as = test_stewardship._as
_metacortex = test_stewardship._metacortex
_person = test_stewardship._person
_project = test_stewardship._project
_sees = test_stewardship._sees


def test_a_departed_owners_public_project_is_its_administrators_to_hide_or_hand_over (
	session: sqlalchemy.orm.Session,
) -> None:
	"""H-1, as measured: keanu's public ``pub``, and keanu taken out of the workspace. **Bob, a
	member, may neither make it private nor make himself its owner**; thomas, who administers the
	workspace, may, and making it private with nobody shared in asks him to make somebody its
	owner first.
	"""

	thomas = _person(session, "thomas")
	keanu = _person(session, "keanu")
	bob = _person(session, "bob")
	workspace = _metacortex(session, thomas, keanu=(keanu, "member"), bob=(bob, "member"))
	pub = _project(session, workspace, "pub", keanu, visibility="public")

	subroutine.domain.workspaces.remove_member(session, workspace, keanu)

	refusals: list[tuple[typing.Callable[[], object], str]] = [
		(
			lambda: projects.update(session, pub, visibility="private", actor=_as(bob)),
			"make it private",
		),
		(
			lambda: projects.update(session, pub, owner_id=bob.id, actor=_as(bob)),
			"change who owns it",
		),
	]

	for refused, act in refusals:
		with pytest.raises(subroutine.errors.Forbidden) as caught:
			refused()

		assert caught.value.detail == f"Only somebody who administers this workspace may {act}."

	assert pub.visibility == "public" and pub.owner_id == keanu.id

	with pytest.raises(subroutine.errors.ValidationError) as stranding:
		projects.update(session, pub, visibility="private", actor=_as(thomas))

	assert stranding.value.detail == "Making pub private would leave no person able to reach it."
	assert stranding.value.hint == "Make somebody who can act its owner first."
	assert pub.visibility == "public"

	projects.update(session, pub, visibility="private", owner_id=thomas.id, actor=_as(thomas))

	assert (
		pub.visibility == "private" and _sees(session, thomas, pub) and not _sees(session, bob, pub)
	)


def test_whoever_can_see_a_project_beneath_a_private_one_still_stands_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""H-1's rule is the private projects at or above, not the project's own visibility: alice's
	private ``vault`` holds keanu's public ``vault/pub``, and an administrator not shared into
	``vault`` cannot see it. Keanu leaves, and bob, shared into ``vault``, takes ``pub`` on.
	"""

	thomas = _person(session, "thomas")
	alice = _person(session, "alice")
	keanu = _person(session, "keanu")
	bob = _person(session, "bob")
	workspace = _metacortex(
		session, thomas, alice=(alice, "member"), keanu=(keanu, "member"), bob=(bob, "member")
	)
	vault = _project(session, workspace, "vault", alice)

	for one in (keanu, bob):
		projects.share(session, vault, one, actor=_as(alice))

	pub = _project(session, workspace, "pub", keanu, visibility="public", parent=vault)

	subroutine.domain.workspaces.remove_member(session, workspace, keanu)

	assert subroutine.domain.stewardship.stewards(session, _as(bob), pub)
	assert not subroutine.domain.stewardship.stewards(session, _as(thomas), pub)

	projects.update(session, pub, owner_id=bob.id, actor=_as(bob))

	assert pub.owner_id == bob.id


def test_once_the_inbox_founder_leaves_a_contributor_still_moves_nobody_elses_work_out_of_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""H-1's third bullet: once the Inbox's founding owner had been removed, charlie, a contributor,
	moved erin's task out of the Inbox into charlie's own private project, and erin and the
	workspace's administrators lost sight of it. **Refused, as it was while the founder was in.**
	"""

	thomas = _person(session, "thomas")
	trinity = _person(session, "trinity")
	erin = _person(session, "erin")
	charlie = _person(session, "charlie")
	workspace = _metacortex(
		session,
		thomas,
		trinity=(trinity, "owner"),
		erin=(erin, "member"),
		charlie=(charlie, "contributor"),
	)
	model = subroutine.db.models.project.Project
	inbox = session.scalars(
		sqlalchemy.select(model).where(model.workspace_id == workspace.id, model.is_inbox.is_(True))
	).one()
	mine = _project(session, workspace, "mine", charlie)
	task = subroutine.domain.tasks.create(
		session, project=inbox, title="Book the venue", assignee_id=erin.id, actor=_as(erin)
	)

	for founder_in in (True, False):
		if not founder_in:
			subroutine.domain.workspaces.remove_member(session, workspace, thomas)

		with pytest.raises(subroutine.errors.Forbidden) as refused:
			subroutine.domain.tasks.update(session, task, project=mine, actor=_as(charlie))

		assert (
			"may move this task into a private project" in refused.value.detail
		), refused.value.detail
		assert task.project_id == inbox.id


def test_making_a_project_private_keeps_somebody_who_can_see_each_private_project_inside (
	session: sqlalchemy.orm.Session,
) -> None:
	"""M-4, as measured: jo makes her public ``outer`` private, and pat's private ``outer/inner``,
	which pat saw without being shared into ``outer``, is seen by nobody. **Refused**, naming
	``inner`` only to somebody who can see it, and nothing changes; handing ``outer`` to pat in the
	same change keeps pat seeing both.
	"""

	thomas = _person(session, "thomas")
	jo = _person(session, "jo")
	pat = _person(session, "pat")
	workspace = _metacortex(session, thomas, jo=(jo, "member"), pat=(pat, "member"))
	outer = _project(session, workspace, "outer", jo, visibility="public")
	inner = _project(session, workspace, "inner", pat, parent=outer)

	with pytest.raises(subroutine.errors.ValidationError) as unseen:
		projects.update(session, outer, visibility="private", actor=_as(jo))

	assert unseen.value.detail == (
		"Making outer private would leave no person able to reach something you cannot see "
		"inside it."
	)
	# **And who sees it, only to somebody who can see it** (`SR#5316`, decision `#5285`).
	assert unseen.value.hint == "Make somebody who can act its owner first."
	assert pat.username not in str(unseen.value), unseen.value

	# **thomas, an administrator pat shared ``inner`` with, is told its key**: he can see it.
	projects.share(session, inner, thomas, actor=_as(pat))

	with pytest.raises(subroutine.errors.ValidationError) as seen:
		projects.update(session, outer, visibility="private", actor=_as(thomas))

	assert (
		seen.value.detail
		== "Making outer private would leave no person able to reach inner inside it."
	)
	assert seen.value.hint == f"Make {pat.username}, who can see inner, the owner of outer first."
	assert outer.visibility == "public" and _sees(session, pat, inner)

	projects.update(session, outer, visibility="private", owner_id=pat.id, actor=_as(jo))

	assert (
		outer.visibility == "private" and _sees(session, pat, outer) and _sees(session, pat, inner)
	)


def test_moving_a_project_under_a_private_one_keeps_somebody_who_sees_each_private_project_inside (
	session: sqlalchemy.orm.Session,
) -> None:
	"""M-4's move: jo moves ``outer`` under her private ``vault``, and pat, not shared into
	``vault``, loses ``outer/inner``, which nobody else sees. **Refused**, the hint the share that
	keeps pat seeing it, or for bob, shared into ``vault`` and not its steward, whom to ask for it;
	once pat is shared in, the move is made.
	"""

	thomas = _person(session, "thomas")
	jo = _person(session, "jo")
	pat = _person(session, "pat")
	bob = _person(session, "bob")
	workspace = _metacortex(
		session, thomas, jo=(jo, "member"), pat=(pat, "member"), bob=(bob, "member")
	)
	vault = _project(session, workspace, "vault", jo)
	outer = _project(session, workspace, "outer", jo, visibility="public")
	inner = _project(session, workspace, "inner", pat, parent=outer)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		projects.move(session, outer, parent=vault, actor=_as(jo))

	assert refused.value.detail == (
		"Moving outer under vault would leave no person able to reach something you cannot see "
		"inside it."
	)
	# **Who sees it, only to somebody who can see it** (`SR#5316`, decision `#5285`).
	assert refused.value.hint == "Share vault with whoever can see what is inside outer first."
	assert outer.parent_id is None and _sees(session, pat, inner)

	projects.share(session, vault, bob, actor=_as(jo))
	den = _project(session, workspace, "den", bob, visibility="public")
	_project(session, workspace, "nook", pat, parent=den)

	with pytest.raises(subroutine.errors.ValidationError) as asked:
		projects.move(session, den, parent=vault, actor=_as(bob))

	assert asked.value.hint == (
		"Ask the owner of 'vault' to share it with whoever can see what is inside den first."
	)
	assert den.parent_id is None

	projects.share(session, vault, pat, actor=_as(jo))
	projects.move(session, outer, parent=vault, actor=_as(jo))

	assert outer.parent_id == vault.id and _sees(session, pat, inner)


def test_a_move_offers_the_share_only_to_whoever_may_share_on_this_credential (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#5316`, L-6 of the cold review of 2026-11-11: the share hint asked stewardship alone.

	jo stewards her private ``vault``, and her credential may change ``outer`` and ``vault/shelf``
	but not ``vault``, so moving ``outer`` under ``shelf`` would hide pat's ``inner`` behind
	``vault``, which this credential would be refused sharing. **She is not offered the share**, as
	`SR#5106` made every other hint ask. **Asked of the refusal directly**: through ``move`` today,
	`SR#5102` refuses a narrowed credential any crossing first, so this is the hint that refusal's
	own order keeps from her, held for the day that order changes.
	"""

	thomas = _person(session, "thomas")
	jo = _person(session, "jo")
	pat = _person(session, "pat")
	workspace = _metacortex(session, thomas, jo=(jo, "member"), pat=(pat, "member"))
	vault = _project(session, workspace, "vault", jo)
	shelf = _project(session, workspace, "shelf", jo, visibility="public", parent=vault)
	outer = _project(session, workspace, "outer", jo, visibility="public")
	_project(session, workspace, "inner", pat, parent=outer)
	_row, issued = subroutine.domain.authentication.issue_token(
		session,
		user=jo,
		title=f"jo's {uuid.uuid4().hex[:6]}",
		project_write_scope=[str(outer.id), str(shelf.id)],
	)
	session.flush()
	narrowed = subroutine.domain.authentication.authenticate(
		session, issued.value.get_secret_value(), record_use=False
	)
	above = tuple(
		uuid.UUID(segment) for segment in subroutine.domain.hierarchy.path_segments(shelf.path)
	)

	assert subroutine.domain.stewardship.stewards(session, narrowed, vault)

	for actor, hint in (
		(_as(jo), "Share vault with whoever can see what is inside outer first."),
		(
			narrowed,
			"Ask the owner of 'vault' to share it with whoever can see what is inside outer first.",
		),
	):
		with pytest.raises(subroutine.errors.ValidationError) as refused:
			projects._refuse_stranding(
				session,
				actor,
				outer,
				afterwards=subroutine.domain.repair.Afterwards(moved=outer.id, above=above),
				doing="Moving outer under shelf",
				field="parent",
			)

		assert refused.value.hint == hint, refused.value.hint


def test_what_nobody_could_see_already_is_not_this_acts_to_refuse (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Refusing only a change from somebody to nobody, as unsharing does: pat has left, so nobody
	sees ``outer/inner`` already, and making ``outer`` private strands nothing new. An administrator
	of the installation is what brings it back.
	"""

	thomas = _person(session, "thomas")
	jo = _person(session, "jo")
	pat = _person(session, "pat")
	workspace = _metacortex(session, thomas, jo=(jo, "member"), pat=(pat, "member"))
	outer = _project(session, workspace, "outer", jo, visibility="public")
	_project(session, workspace, "inner", pat, parent=outer)

	subroutine.domain.users.set_active(session, pat, active=False)
	projects.update(session, outer, visibility="private", actor=_as(jo))

	assert outer.visibility == "private"


def test_making_a_project_private_over_http_is_refused_on_its_field_naming_nothing_unseen (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The same refusal through the API: 422 on ``visibility``, and the hidden project's key nowhere
	in it - **nor, since `SR#5316`, who alone sees it** (decision `#5285`): that is an entry of a
	sharing list the caller cannot read.
	"""

	world = test_api_tasks._world(session)
	pat = subroutine.domain.users.create(session, username=f"pat-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, pat, role_key="member")
	outer = projects.create(
		session, workspace_id=world.workspace.id, key="outer", title="Outer", owner_id=world.user.id
	)
	projects.create(
		session,
		workspace_id=world.workspace.id,
		key="hush",
		title="Hush",
		parent=outer,
		visibility="private",
		owner_id=pat.id,
	)
	session.flush()

	answer = world.call("PATCH", "/v1/projects/outer", json={"visibility": "private"})

	assert answer.status_code == 422, answer.text
	assert answer.json()["errors"][0]["field"] == "visibility", answer.text
	assert "hush" not in answer.text.lower(), answer.text
	assert pat.username not in answer.text, answer.text
	assert answer.json()["hint"] == "Make somebody who can act its owner first.", answer.text
	assert world.call("GET", "/v1/projects/outer").json()["visibility"] == "public"
