"""Tests for local-mode identity, tag auto-creation, and capture reaching the database.

SPEC.md §12.1a's resolution order is the interesting part, and the clause worth having is
the third: **a token narrows local mode**. Without it an agent invoking the CLI against the
database directly holds unrestricted authority over everything in it, which is precisely
the posture §14.12 warns about — and it would be an easy thing to leave until the API,
where it would then have to be retrofitted into a path that had never had it.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.bootstrap
import subroutine.domain.local
import subroutine.domain.projects
import subroutine.domain.tags
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions


def _installed (
	session: sqlalchemy.orm.Session, *, username: str = "si"
) -> subroutine.domain.bootstrap.Bootstrap:
	"""Set up an installation the way ``subroutine init`` does."""

	return subroutine.domain.bootstrap.initialise(
		session,
		username=f"{username}-{uuid.uuid4().hex[:8]}",
		instance_name="Test instance",
		timezone="Europe/London",
	)


def test_the_only_account_is_who_you_are (session: sqlalchemy.orm.Session) -> None:
	"""The ordinary personal case asks nothing of anybody (SPEC.md §12.1a, rule 2)."""

	installed = _installed(session)
	principal = subroutine.domain.local.principal(session)

	assert principal.user.id == installed.user.id
	assert principal.token is None


def test_more_than_one_account_stops_and_lists_them (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Guessing whose to-do list is on screen is not an error that announces itself."""

	installed = _installed(session)
	other = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.local.principal(session)

	assert raised.value.errors[0].field == "local_user"
	assert raised.value.hint is not None
	assert installed.user.username in raised.value.hint
	assert other.username in raised.value.hint


def test_local_user_names_which_account_to_be (session: sqlalchemy.orm.Session) -> None:
	"""Rule 3: the configuration settles it."""

	_installed(session)
	other = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")

	principal = subroutine.domain.local.principal(session, local_user=other.username)

	assert principal.user.id == other.id


def test_a_local_user_that_does_not_exist_says_who_does (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A typo in a config file should name the alternatives, not just fail."""

	installed = _installed(session)

	with pytest.raises(subroutine.errors.NotFound) as raised:
		subroutine.domain.local.principal(session, local_user="nobody")

	assert raised.value.hint is not None
	assert installed.user.username in raised.value.hint


def test_a_token_narrows_local_mode (session: sqlalchemy.orm.Session) -> None:
	"""Rule 1, and the reason it exists.

	Hand an agent a scoped token and the CLI constrains it with no server involved. The
	alternative — unrestricted authority whenever an agent invokes the CLI — is the posture
	this rule exists to prevent (SPEC.md §12.1a, §14.12).
	"""

	installed = _installed(session)
	token, issued = subroutine.domain.authentication.issue_token(
		session,
		user=installed.user,
		title="Agent",
		scopes=[subroutine.permissions.TASK_READ],
	)

	principal = subroutine.domain.local.principal(
		session, token=issued.value.get_secret_value()
	)

	assert principal.user.id == installed.user.id
	assert principal.token is not None
	assert principal.scopes == [subroutine.permissions.TASK_READ]
	assert token.id == principal.token.id


def test_a_token_that_cannot_be_used_is_an_error_not_a_fallback (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A credential that stops narrowing when it lapses is worse than no credential.

	Falling through to "the only user" would mean an agent whose token expired carries on
	working, with *more* authority than it had a moment earlier.
	"""

	_installed(session)

	with pytest.raises(subroutine.errors.Unauthenticated) as raised:
		subroutine.domain.local.principal(session, token="sr_deadbeef_nonsense")

	assert raised.value.hint is not None
	assert "SUBROUTINE_TOKEN" in raised.value.detail


def test_a_pinned_token_narrows_which_workspace_local_mode_uses (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The pin does its work here, the same way it will over HTTP."""

	installed = _installed(session)
	second = subroutine.domain.workspaces.create(
		session,
		slug=f"ws-{uuid.uuid4().hex[:8]}",
		title="Second",
		owner=installed.user,
	)
	_token, issued = subroutine.domain.authentication.issue_token(
		session, user=installed.user, title="Pinned", workspace_id=second.id
	)

	principal = subroutine.domain.local.principal(
		session, token=issued.value.get_secret_value()
	)

	assert subroutine.domain.local.workspace_for(session, principal).id == second.id
	assert subroutine.domain.local.readable_workspace_ids(session, principal) == [second.id]


def test_the_workspace_is_the_oldest_one_you_belong_to (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Personal commands never name a workspace, so this is where the word stops."""

	installed = _installed(session)
	subroutine.domain.workspaces.create(
		session,
		slug=f"ws-{uuid.uuid4().hex[:8]}",
		title="Later",
		owner=installed.user,
	)
	principal = subroutine.domain.local.principal(session)

	assert subroutine.domain.local.workspace_for(session, principal).id == installed.workspace.id


def test_a_tag_is_created_the_first_time_it_is_used (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Typing ``#health`` should not require a tag-management step (SPEC.md §5.8)."""

	installed = _installed(session)

	first = subroutine.domain.tags.ensure(
		session, workspace_id=installed.workspace.id, names=["health"]
	)
	again = subroutine.domain.tags.ensure(
		session, workspace_id=installed.workspace.id, names=["Health"]
	)

	assert [tag.id for tag in first] == [tag.id for tag in again]
	assert first[0].name == "health"


def test_an_all_digit_tag_is_refused_at_the_service_and_not_only_by_the_parser (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``#`` means a tag and a reference, and only one of them can be all digits.

	The capture grammar already declines to read ``#42`` as a tag, so nothing in the
	application reaches this today — which is exactly why it is worth a test. The rule lived
	in two regexes and would not have travelled to the ``tags`` field the API is going to
	grow. A tag named "42" could never be written with its own sigil, and ``#42`` in anybody
	else's description would go on meaning item 42.
	"""

	installed = _installed(session)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tags.ensure(
			session, workspace_id=installed.workspace.id, names=["42"]
		)

	assert refused.value.errors[0].field == "tags"
	assert "#42" in (refused.value.errors[0].hint or ""), "the refusal says what it collides with"

	# Digits are fine as long as the name is not only digits.
	allowed = subroutine.domain.tags.ensure(
		session, workspace_id=installed.workspace.id, names=["2fa", "3d-printing"]
	)

	assert [tag.name for tag in allowed] == ["2fa", "3d-printing"]


def test_tag_names_collapse_by_their_normalised_form (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``#health #Health`` is one tag, not an error about a convenience feature."""

	installed = _installed(session)

	tags = subroutine.domain.tags.ensure(
		session, workspace_id=installed.workspace.id, names=["Health", "health", "  HEALTH "]
	)

	assert len(tags) == 1


def test_a_captured_line_becomes_a_task_with_its_tags (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§6.13 through to the database, which is what S2-03 deliberately left undone."""

	installed = _installed(session)
	principal = subroutine.domain.authentication.Principal(user=installed.user)

	task, captured = subroutine.domain.tasks.create_from_text(
		session,
		workspace=installed.workspace,
		text="Write the report by friday !3 ~2h #work",
		actor=principal,
	)

	assert task.title == "Write the report"
	assert task.importance == 3
	assert task.estimate_minutes == 120
	assert captured.tags == ("work",)
	assert [tag.name for tag in subroutine.domain.tags.for_task(session, task)] == ["work"]

	# No project named, so it lands in the Inbox — the one project the personal path never
	# mentions (SPEC.md §6.8).
	assert task.project_id == installed.inbox.id


def test_a_named_project_is_used_and_a_wrong_one_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``+WEB`` naming nothing is a typo; filing it in the Inbox anyway would lose it."""

	installed = _installed(session)
	project = subroutine.domain.projects.create(
		session, workspace_id=installed.workspace.id, key="WEB", title="Website"
	)

	task, _captured = subroutine.domain.tasks.create_from_text(
		session, workspace=installed.workspace, text="Fix the build +WEB"
	)

	assert task.project_id == project.id

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.tasks.create_from_text(
			session, workspace=installed.workspace, text="Fix the build +NOPE"
		)

	assert raised.value.errors[0].field == "project"
	assert raised.value.errors[0].hint is not None
	assert "WEB" in raised.value.errors[0].hint


def test_an_unknown_assignee_names_the_members (session: sqlalchemy.orm.Session) -> None:
	"""``@nobody`` is a typo, and the remedy is the list of people who are here."""

	installed = _installed(session)

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.tasks.create_from_text(
			session, workspace=installed.workspace, text="Review it @nobody"
		)

	assert raised.value.errors[0].field == "assignee"
	assert raised.value.errors[0].hint is not None
	assert installed.user.username in raised.value.errors[0].hint


def test_structured_fields_win_over_parsed_ones (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §6.13: a client that wants no magic simply does not send text worth parsing.

	The capture still runs, so the title still loses the token — otherwise supplying
	``importance`` explicitly would leave a stray ``!3`` behind in it.
	"""

	installed = _installed(session)

	task, _captured = subroutine.domain.tasks.create_from_text(
		session,
		workspace=installed.workspace,
		text="Write the report !3",
		importance=5,
	)

	assert task.importance == 5
	assert task.title == "Write the report"


@pytest.mark.parametrize(
	"description", ["a token principal", "a plain principal"]
)
def test_describe_says_who_is_acting_without_printing_the_token (
	session: sqlalchemy.orm.Session, description: str
) -> None:
	"""``doctor`` and ``--verbose`` need this, and neither may leak the credential."""

	installed = _installed(session)
	principal: typing.Any = subroutine.domain.authentication.Principal(user=installed.user)

	if description == "a token principal":
		_token, issued = subroutine.domain.authentication.issue_token(
			session,
			user=installed.user,
			title="Agent",
			scopes=[subroutine.permissions.TASK_READ],
		)
		principal = subroutine.domain.local.principal(
			session, token=issued.value.get_secret_value()
		)

		assert issued.value.get_secret_value() not in subroutine.domain.local.describe(principal)

	assert installed.user.username in subroutine.domain.local.describe(principal)


def test_a_deactivated_account_cannot_be_named_by_local_user (
	session: sqlalchemy.orm.Session,
) -> None:
	"""All three ways to become a principal agree about deactivation.

	`_named` filtered only `deleted_at` until the slice-2 review, so somebody could leave,
	have their account deactivated, and a `local_user` line left in a configuration file
	would go on working.
	"""

	_installed(session)
	departed = subroutine.domain.users.create(
		session, username=f"departed-{uuid.uuid4().hex[:8]}"
	)
	departed.is_active = False
	session.flush()

	with pytest.raises(subroutine.errors.NotFound):
		subroutine.domain.local.principal(session, local_user=departed.username)


def test_a_read_only_token_cannot_write (session: sqlalchemy.orm.Session) -> None:
	"""The claim §12.1a makes, asserted against the service rather than the principal.

	The slice-2 review found this false: `authorize()` existed and nothing called it, so a
	token scoped to `task:read` created tasks freely. The test that existed asserted
	`principal.scopes == ["task:read"]` — true, and evidence of nothing.
	"""

	installed = _installed(session)
	_token, issued = subroutine.domain.authentication.issue_token(
		session,
		user=installed.user,
		title="Agent",
		scopes=[subroutine.permissions.TASK_READ],
	)
	principal = subroutine.domain.local.principal(
		session, token=issued.value.get_secret_value()
	)

	with pytest.raises(subroutine.errors.Forbidden) as raised:
		subroutine.domain.tasks.create(
			session, project=installed.inbox, title="Should be refused", actor=principal
		)

	assert "task:write" in raised.value.detail

	# And the read half still works, or the token would be useless rather than narrow.
	assert subroutine.domain.authorization.may(
		session,
		principal,
		subroutine.permissions.TASK_READ,
		workspace_id=installed.workspace.id,
	)


def test_a_stranger_to_the_workspace_cannot_create_a_task (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Membership is the floor for writing, not only for reading."""

	installed = _installed(session)
	outsider = subroutine.domain.users.create(
		session, username=f"outsider-{uuid.uuid4().hex[:8]}"
	)

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.tasks.create(
			session,
			project=installed.inbox,
			title="Not yours",
			actor=subroutine.domain.authentication.Principal(user=outsider),
		)


def test_creating_a_workspace_needs_the_instance_permission (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The instance tier had no call site until now (SPEC.md §7.1, Appendix A)."""

	installed = _installed(session)
	ordinary = subroutine.domain.users.create(
		session, username=f"member-{uuid.uuid4().hex[:8]}"
	)

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.workspaces.create(
			session,
			slug=f"ws-{uuid.uuid4().hex[:8]}",
			title="Not allowed",
			owner=ordinary,
			actor=subroutine.domain.authentication.Principal(user=ordinary),
		)

	# The account `init` created is a superuser, so it may.
	second = subroutine.domain.workspaces.create(
		session,
		slug=f"ws-{uuid.uuid4().hex[:8]}",
		title="Allowed",
		owner=installed.user,
		actor=subroutine.domain.authentication.Principal(user=installed.user),
	)

	assert second.id is not None


def test_privacy_reaches_a_private_projects_children (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Settled 2026-07-29: private inherits down the tree (SPEC.md §7.3a).

	Before this, marking a project private and creating a sub-project inside it published
	the sub-project's titles to the whole workspace — while the token-scope rule beside it
	applied the subtree reading. Two controls, same shape, opposite answers.
	"""

	installed = _installed(session)
	principal = subroutine.domain.authentication.Principal(user=installed.user)

	private = subroutine.domain.projects.create(
		session,
		workspace_id=installed.workspace.id,
		key="PRIV",
		title="Private",
		visibility="private",
	)
	child = subroutine.domain.projects.create(
		session,
		workspace_id=installed.workspace.id,
		key="CHILD",
		title="Public child",
		parent=private,
		visibility="public",
	)

	assert not subroutine.domain.authorization.is_visible(session, principal, private)
	assert not subroutine.domain.authorization.is_visible(session, principal, child)

	session.add(
		subroutine.db.models.project.ProjectMember(
			project_id=private.id,
			user_id=installed.user.id,
			workspace_id=installed.workspace.id,
		)
	)
	session.flush()

	assert subroutine.domain.authorization.is_visible(session, principal, private)
	assert subroutine.domain.authorization.is_visible(session, principal, child)
