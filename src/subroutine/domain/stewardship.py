"""Who manages what is owned, and who decides who sees work - one rule for both.

`SR#4572` and `SR#4570`, decisions `#4525`, `#4509` and `#4516`, on R5 of the cold review of
2026-10-05. **Eight helpers each asked part of this**, each with its own idea of who else may, its
own order and its own wording: deciding who sees a project, the owner's share, a comment's author, a
saved view's owner, a credential's holder or issuer, a feed's owner, signing somebody out, and each
administrator's branch of a delete.

**An owned thing is managed by its owner, by whoever its owner answers to while it can act, or by
the administrator of its place.** *Managing* is deciding its audience, handing it over and removing
it, and for a credential stopping it:

* a project, a saved view or a comment - ``workspace:admin`` in its workspace, held by somebody who
  can see it, so a private project stays hidden from an administrator who is not in it;
* a credential - an API token, a sign-in link, a browser session or a calendar feed - the
  installation's accounts administrator, ``instance:user_create`` (decision `#4524`).

**Authored text is rewritten only by its author** - a comment's body, a saved view's definition -
and **only a credential's holder mints or resets it**. An administrator may remove words, never
rewrite them.

**A project's steward decides who sees the work in it** (decision `#4509`): making it private or
public, sharing it or removing somebody else's share, changing its owner, and moving a project, a
task or a document across a privacy boundary. **When the owner can no longer act**, anybody who can
see the project stands in where it, or a project above it, is private, so a departed owner's private
project is never stranded (`SR#5099`); an owner nobody names cannot act either, and a public project
whose owner cannot act is its administrators'.

The act's own verb is asked first, by its caller, so a refusal here is only ever about whose the
thing is. ``tests/test_stewardship.py`` holds that nothing outside this module compares an actor with
an owner.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.hierarchy
import subroutine.errors
import subroutine.permissions


def holds (
	actor: subroutine.domain.authentication.Principal | None, owner_id: uuid.UUID | None
) -> bool:
	"""Report whether the actor is the account an owner column names. ``None`` is an internal caller."""

	return actor is None or (owner_id is not None and owner_id == actor.user.id)


def answers_for (
	session: sqlalchemy.orm.Session,
	owner: subroutine.db.models.identity.User,
	actor: subroutine.domain.authentication.Principal,
) -> bool:
	"""Report whether the actor is ``owner``, or somebody ``owner`` answers to - decision `#4516`.

	**Never downward**: an agent gains nothing over the account it answers to.
	"""

	return owner.id == actor.user.id or subroutine.domain.accountability.answers_to(
		session, owner, actor.user
	)


def managed_owners (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
) -> set[uuid.UUID]:
	"""Return the accounts whose things in a workspace this actor manages as their owner's own.

	**:func:`manages` for a query** (decision `#4525`): itself, and every agent answering to it that
	can act there, as ``model.owner_id.in_(...)``. Its administrator's half is the caller's, since
	what an administrator may see differs by kind.
	"""

	return {actor.user.id} | {
		agent.id
		for agent in subroutine.domain.accountability.agents_answering_to(session, actor.user)
		if subroutine.domain.accountability.can_act(session, agent, workspace_id=workspace_id)
	}


def _owner (
	session: sqlalchemy.orm.Session, owner_id: uuid.UUID | None
) -> subroutine.db.models.identity.User | None:
	"""Return the account an owner column names, or ``None``."""

	return None if owner_id is None else session.get(subroutine.db.models.identity.User, owner_id)


def _acting_owner (
	session: sqlalchemy.orm.Session, owner_id: uuid.UUID | None, *, workspace_id: uuid.UUID
) -> subroutine.db.models.identity.User | None:
	"""Return the owner where it can act in this workspace, or ``None`` where it cannot or is nobody."""

	owner = _owner(session, owner_id)

	if owner is None or not subroutine.domain.accountability.can_act(
		session, owner, workspace_id=workspace_id
	):
		return None

	return owner


def _administers (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None,
) -> bool:
	"""Report whether the actor administers this workspace and can see the project the thing is in.

	**``workspace:admin`` lands on the workspace** (decision `#4527`), so it is asked there and a
	credential narrowed to some projects administers nothing here; **sight is asked of the project**,
	so an administrator not shared into a private project does not reach it, as nobody would. A thing
	outside every project - a saved view shared with the workspace - is asked of the workspace alone.
	"""

	if (
		subroutine.domain.authorization.refusal(
			session, actor, subroutine.permissions.WORKSPACE_ADMIN, workspace_id=workspace_id
		)
		is not None
	):
		return False

	return (
		project is None
		or subroutine.domain.authorization.refusal(
			session,
			actor,
			subroutine.permissions.PROJECT_READ,
			workspace_id=workspace_id,
			project=project,
		)
		is None
	)


def manages (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	owner_id: uuid.UUID | None,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None = None,
	seen: bool = True,
) -> bool:
	"""Report whether this actor manages a thing owned by ``owner_id`` in a workspace - decision `#4525`.

	Its owner, or whoever the owner answers to while it can act there, or an administrator of the
	workspace who can see the thing: ``project`` is the one it is in, and ``seen`` is false for a
	thing nobody but its owner sees, such as a view nobody shared.
	"""

	if actor is None:
		return True

	owner = _acting_owner(session, owner_id, workspace_id=workspace_id)

	if owner is not None and answers_for(session, owner, actor):
		return True

	return seen and _administers(session, actor, workspace_id=workspace_id, project=project)


def refuse_unless_managing (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	owner_id: uuid.UUID | None,
	*,
	workspace_id: uuid.UUID,
	whose: str,
	act: str,
	field: str,
	project: subroutine.db.models.project.Project | None = None,
	seen: bool = True,
) -> None:
	"""Refuse managing a thing unless :func:`manages` - one sentence for every kind, `SR#4572`.

	``whose`` names the thing's owner as a reader knows it - *the owner of 'web'*, *the author of this
	comment* - and ``act`` what was refused, so the sentence names who may.
	"""

	if manages(session, actor, owner_id, workspace_id=workspace_id, project=project, seen=seen):
		return

	_refuse(
		_acting_owner(session, owner_id, workspace_id=workspace_id),
		whose=whose,
		act=act,
		field=field,
		message="What is owned is managed by its owner, or by somebody who administers this "
		"workspace.",
	)


def _refuse (
	owner: subroutine.db.models.identity.User | None,
	*,
	whose: str,
	act: str,
	field: str,
	message: str,
) -> typing.NoReturn:
	"""Raise the one refusal naming who may: the owner where it can act, and an administrator."""

	if owner is None:
		detail = f"Only somebody who administers this workspace may {act}."
		hint = "Ask somebody who administers this workspace."

	elif owner.is_service_account:
		detail = (
			f"Only {whose}, whoever that agent answers to, or somebody who administers this "
			f"workspace, may {act}."
		)
		hint = f"Ask whoever {owner.username} answers to, or somebody who administers this workspace."

	else:
		detail = f"Only {whose}, or somebody who administers this workspace, may {act}."
		hint = f"Ask {owner.username}, or somebody who administers this workspace."

	raise subroutine.errors.Forbidden(
		detail,
		errors=[subroutine.errors.FieldError(field=field, code="forbidden", message=message)],
		hint=hint,
	)


def refuse_unless_author (
	actor: subroutine.domain.authentication.Principal | None,
	author_id: uuid.UUID | None,
	*,
	detail: str,
	hint: str,
	field: str | None = None,
	message: str | None = None,
) -> None:
	"""Refuse rewriting authored text by anybody but its author - decision `#4525`.

	**Not whoever the author answers to, and not an administrator**: words carry their author's name,
	and somebody else's rewriting them under it is not a permission anybody should hold. Removing
	them is the honest alternative, and is :func:`manages`'s.
	"""

	if holds(actor, author_id):
		return

	if field is None or message is None:
		raise subroutine.errors.Forbidden(detail, hint=hint)

	raise subroutine.errors.Forbidden(
		detail,
		errors=[
			subroutine.errors.FieldError(field=field, code="forbidden", message=message, hint=hint)
		],
	)


def refuse_unless_holding (
	actor: subroutine.domain.authentication.Principal | None,
	holder_id: uuid.UUID,
	*,
	detail: str,
	field: str,
	hint: str,
) -> None:
	"""Refuse minting or resetting a credential for anybody but its holder - decision `#4525`.

	**Checked inside the act** (C section 2.6 of the cold review of 2026-10-05): resetting a calendar
	feed trusted the lookup that found it to have asked, so a caller resolving a feed another way
	would have given somebody else's feed a new address reading with their sight.
	"""

	if holds(actor, holder_id):
		return

	raise subroutine.errors.Forbidden(
		detail,
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="forbidden",
				message="A new address reads with its holder's own sight, so only they may ask for one.",
			)
		],
		hint=hint,
	)


def stewards (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether this actor is the steward of a project - decision `#4509`.

	**Its owner, or anybody the owner answers to while the owner can act, or a workspace
	administrator who can see it.** When the owner cannot act - deactivated, out of the workspace, or
	an agent whose chain has stopped - or nobody owns it, **anybody who can see it stands in where it,
	or a project above it, is private** (`SR#5099`), so a departed owner's private project is never
	stranded: no administrator sees a private project they are not in, and the remaining members
	still reach it, so no rescue applies either (C section 2.4 of the cold review of 2026-10-05,
	measured on the verification's mutant). **Not the project's own visibility**: a public project
	under a private one is hidden from every administrator not shared into that, which is the case
	the stand-in exists for.
	"""

	if actor is None:
		return True

	owner = _acting_owner(session, project.owner_id, workspace_id=project.workspace_id)

	# **A public project whose owner cannot act is its administrators'** (`SR#5099`, H-1 of the cold
	# review of 2026-10-17, decision `#5097`): with the stand-in asking nothing of its privacy, any
	# member could make one private, or take it over and then hide it, and a contributor could move
	# somebody else's work out of an Inbox whose founder had left into their own private project.
	if owner is None and not private_above(session, project):
		return _administers(session, actor, workspace_id=project.workspace_id, project=project)

	if owner is None:
		return (
			subroutine.domain.authorization.refusal(
				session,
				actor,
				subroutine.permissions.PROJECT_READ,
				workspace_id=project.workspace_id,
				project=project,
			)
			is None
		)

	if answers_for(session, owner, actor):
		return True

	return _administers(session, actor, workspace_id=project.workspace_id, project=project)


def refuse_unless_steward (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	project: subroutine.db.models.project.Project,
	*,
	act: str,
	field: str,
) -> None:
	"""Refuse deciding who sees a project's work unless by its steward - `SR#4570`, decision `#4509`."""

	if stewards(session, actor, project):
		return

	_refuse(
		_acting_owner(session, project.owner_id, workspace_id=project.workspace_id),
		whose=f"the owner of '{project.key}'",
		act=act,
		field=field,
		message="Who can see a project's work, and who owns it, are its owner's to decide, or an "
		"administrator's.",
	)


def private_above (
	session: sqlalchemy.orm.Session, project: subroutine.db.models.project.Project | None
) -> frozenset[uuid.UUID]:
	"""Return the private projects at or above this one - who can see what is in it, by its privacy.

	**The privacy boundary is this set** (decision `#4509`): a move crosses one where the set differs
	between where the work is and where it goes. ``None`` is the top level, which no private project
	is above.
	"""

	if project is None:
		return frozenset()

	identifiers = [
		uuid.UUID(segment) for segment in subroutine.domain.hierarchy.path_segments(project.path)
	]

	model = subroutine.db.models.project.Project

	return frozenset(
		session.scalars(
			sqlalchemy.select(model.id).where(model.id.in_(identifiers), model.visibility == "private")
		)
	)


def refuse_crossing (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	*,
	steward_of: subroutine.db.models.project.Project,
	leaving: frozenset[uuid.UUID],
	arriving: frozenset[uuid.UUID],
	what: str,
	field: str,
	under: bool = False,
) -> None:
	"""Refuse moving work across a privacy boundary unless by the steward - `SR#4570`, decision `#4509`.

	**A move crosses one where the private projects above the work differ before and after** - into a
	private project, out of one, or from one private tree to another. One that crosses none takes
	what it took before, so a move within one private tree is no longer refused, which nobody's sight
	changes (NEW-O-1 of the verification of 2026-10-05). ``steward_of`` is the project whose steward
	decides: a moved project's own, or for a task or a document the project it leaves. ``under`` is
	how a project is said to move, beneath another rather than into it.
	"""

	if leaving == arriving:
		return

	if not arriving:
		where = "out from under a private project" if under else "out of a private project"

	elif not leaving:
		where = "under a private project" if under else "into a private project"

	else:
		where = "under a different private project" if under else "into a different private project"

	refuse_unless_steward(session, actor, steward_of, act=f"move {what} {where}", field=field)


def refuse_narrowed_crossing (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	*,
	workspace_id: uuid.UUID,
	leaving: frozenset[uuid.UUID],
	arriving: frozenset[uuid.UUID],
	permission: str,
	field: str,
) -> None:
	"""Refuse a move across a privacy boundary to a credential narrowed to some projects - `SR#5102`.

	**A credential narrowed to some projects makes no crossing move** (M-5 of the cold review of
	2026-10-17, decision `#5097`): it may not make a project private or public, and moved one, a task
	or a document across the same boundary, changing who sees it as surely. So a crossing asks the
	workspace's place, as changing a project's visibility does, and a narrowed agent asks somebody
	whose credential reaches the whole workspace. **Asked by each caller after the rules' own
	refusals**, whose reasons are the more particular: asked first, it shadowed them.
	"""

	if actor is None or leaving == arriving:
		return

	subroutine.domain.authorization.authorize(
		session,
		actor,
		permission,
		workspace_id=workspace_id,
		instead="Ask somebody whose credential reaches the whole workspace to move it.",
		field=field,
	)


def refuse_acting_on_an_account (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	account: subroutine.db.models.identity.User,
	*,
	stopping: bool,
	its_own: bool = True,
	reading: bool = False,
) -> None:
	"""Refuse acting on ``account`` unless this actor may - `SR#4566`, decisions `#4516` and `#4524`.

	**One rule for every act on an account**, R4 of the cold review of 2026-10-05, where three
	helpers each asked part of it, and **the credential half of the ownership rule** (`SR#4572`,
	decision `#4525`): an account's credentials are managed by it, by whoever it answers to, and by
	the instance's accounts administrator.

	* **Your own account is yours** (``its_own``): issuing for yourself, revoking your own
	  credentials, signing yourself out. Marking yourself as having left is not, and says so.
	* **Whoever an account answers to may stop it** (``stopping``): revoke its credentials, sign it
	  out, mark it as having left. Stopping only removes, so it takes nothing from anybody but the
	  account stopped. Holding an agent granted nothing over it (`#4159`), so its person could not
	  even revoke a credential of its they had not issued.
	* **Starting anything for another account is the instance tier's**, ``instance:user_create``,
	  described as *accounts* (decision `#4524`): issuing for it, bringing it back, and every act on
	  an account that does not answer to the actor. An answerer issuing for its agent would escalate
	  wherever the agent's role is wider than its person's.

	**An agent is held to its own agents before this is asked**, by
	:func:`subroutine.domain.accountability.refuse_a_person_act`, so nothing here asks what kind of
	account the actor is. ``reading`` is a listing's question, which a read-only session may ask.
	"""

	if actor is None:
		return

	if its_own and holds(actor, account.id):
		return

	if stopping and subroutine.domain.accountability.answers_to(session, account, actor.user):
		return

	subroutine.domain.authorization.authorize_instance(
		actor, subroutine.permissions.INSTANCE_USER_CREATE, reading=reading
	)


def accounts_acted_on (
	session: sqlalchemy.orm.Session, actor: subroutine.domain.authentication.Principal
) -> set[uuid.UUID] | None:
	"""Return the accounts whose credentials this actor may list, or ``None`` for every account.

	**What :func:`refuse_acting_on_an_account` admits, for a listing** (`SR#4566`): its own, the
	accounts answering to it, and with ``instance:user_create`` everybody's. A listing shows what its
	reader could act on, so an inventory never offers what revoking would refuse.
	"""

	if subroutine.domain.authorization.may_instance(
		actor, subroutine.permissions.INSTANCE_USER_CREATE, reading=True
	):
		return None

	return {actor.user.id} | subroutine.domain.accountability.answering_to(session, actor.user)
