"""A credential acts on another only where it dominates it - `SR#4560`, decision `#4527`.

**Dominance** is holding at least the other's verbs and places, and its expiry when minting or
resetting it: a credential handed back must not outlive the one that asked (`#356`), while
stopping one counts no expiry, so a fortnight's browser session still revokes a permanent token.
It replaced a blunt rule that refused every narrowed credential every act on another credential
(`#829`, decision `#3914`), and the five clauses issuing a token asked are its axes.
"""

import datetime
import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.tokens
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors


def _place (
	session: sqlalchemy.orm.Session,
) -> tuple[
	subroutine.db.models.identity.User,
	subroutine.db.models.identity.Workspace,
	subroutine.db.models.project.Project,
	subroutine.db.models.project.Project,
]:
	"""Return a person, their workspace and two of its projects."""

	owner = subroutine.domain.users.create(session, username=f"trinity-{uuid.uuid4().hex[:8]}")
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"metacortex-{uuid.uuid4().hex[:6]}", title="MetaCortex", owner=owner
	)
	web = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="web", title="Website rebuild"
	)
	ops = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="ops", title="Operations"
	)

	return owner, workspace, web, ops


def _presenting (
	session: sqlalchemy.orm.Session,
	owner: subroutine.db.models.identity.User,
	**narrowing: typing.Any,
) -> tuple[subroutine.domain.authentication.Principal, subroutine.db.models.identity.ApiToken]:
	"""Issue the owner a credential narrowed as asked, and return them presenting it, with it."""

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=owner, title=f"Token {uuid.uuid4().hex[:6]}", **narrowing
	)
	session.flush()

	return subroutine.domain.authentication.Principal(user=owner, token=token), token


def test_a_credential_revokes_and_lists_its_owners_credentials_only_where_it_dominates_them (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A narrowed credential revokes a narrower one of its owner's, and not a wider one.

	It revoked none of them under the blunt rule but itself, and listed all of them, wider ones
	included - an inventory it could read and not act on. **The listing is what it dominates now**,
	itself among them, and revoking a wider one is refused with the axis that stood in the way.
	"""

	owner, _workspace, web, ops = _place(session)
	narrowed, held = _presenting(session, owner, project_scope=[str(web.id)])
	_wide, wide = _presenting(session, owner)
	_other, elsewhere = _presenting(session, owner, project_scope=[str(ops.id)])
	_narrower, narrower = _presenting(
		session, owner, project_scope=[str(web.id)], scopes=["task:read"]
	)

	listed = {row.id for row in subroutine.domain.tokens.issued_tokens(session, actor=narrowed)}

	assert listed == {held.id, narrower.id}, "it listed what it does not dominate"

	for wider in (wide, elsewhere):
		with pytest.raises(subroutine.errors.Forbidden) as refused:
			subroutine.domain.tokens.revoke(session, wider, actor=narrowed)

		assert refused.value.errors[0].field == "project_scope", refused.value.errors
		assert wider.revoked_at is None

	subroutine.domain.tokens.revoke(session, narrower, actor=narrowed)

	assert narrower.revoked_at is not None


def test_stopping_counts_no_expiry_and_minting_does (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A fortnight's credential revokes a permanent one and does not mint one.

	Counting the expiry for stopping would stop a browser session, which lasts a fortnight,
	revoking a token from the settings page (measured in `#4507`, area C); not counting it for
	minting would let a credential outlive itself (`#356`).
	"""

	owner, _workspace, _web, _ops = _place(session)
	fortnight = subroutine.db.types.utcnow() + datetime.timedelta(days=14)
	brief, _held = _presenting(session, owner, expires_at=fortnight)
	_permanent, permanent = _presenting(session, owner)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.authentication.issue_token(
			session, user=owner, title="For ever", actor=brief
		)

	assert refused.value.errors[0].field == "expires", refused.value.errors

	subroutine.domain.tokens.revoke(session, permanent, actor=brief)

	assert permanent.revoked_at is not None


def test_a_credential_that_only_reads_is_compared_on_what_it_reads (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Where it may write is never asked of a credential that changes nothing.

	A credential reading ``web`` and ``ops`` and writing only in ``web`` was refused a read-only
	token for ``ops``, because the write set was compared whatever the token could do. It reads
	no more than its maker does, so it is issued; a token that writes in ``ops`` is still refused.
	"""

	owner, _workspace, web, ops = _place(session)
	both = [str(web.id), str(ops.id)]
	writing_web, _held = _presenting(
		session, owner, project_scope=both, project_write_scope=[str(web.id)]
	)

	subroutine.domain.authentication.issue_token(
		session,
		user=owner,
		title="Reads ops",
		scopes=["task:read"],
		project_scope=[str(ops.id)],
		actor=writing_web,
	)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.authentication.issue_token(
			session,
			user=owner,
			title="Writes ops",
			scopes=["task:read", "task:write"],
			project_scope=[str(ops.id)],
			actor=writing_web,
		)

	assert refused.value.errors[0].field == "project_write_scope", refused.value.errors


def test_a_pinned_credential_issuing_an_unpinned_one_is_told_a_field_it_can_send (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4560`: the pin's refusal named ``workspace_id``, which ``POST /v1/tokens`` does not take.

	The body's field is ``workspace``, as the flag is ``--workspace``, so the field the refusal
	named was one no caller could send to put it right.
	"""

	owner, workspace, _web, _ops = _place(session)
	pinned, _held = _presenting(session, owner, workspace_id=workspace.id)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.authentication.issue_token(
			session, user=owner, title="Everywhere", actor=pinned
		)

	assert refused.value.errors[0].field == "workspace", refused.value.errors
