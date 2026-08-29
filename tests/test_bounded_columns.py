"""Every bounded column a caller can write refuses what will not fit — item ``#1571``.

**Two declarations of one width, in modules that cannot see each other.** A column says how
much it will hold; a domain module says what it will accept. They agreed on the day each was
written and nothing compares them, which is this codebase's signature defect — and the failure
is not an exception. PostgreSQL raises where SQLite truncates or stores, so a row written on a
laptop is one production refuses, and ``db copy`` reports it naming no table, column or row.

**The register is the point, not the loop.** Of the bounded ``String`` columns in the schema,
most are ours — a token hash, a discriminator, a derived path — and want a written reason
rather than a driver. The rest are somebody's typing, and each is driven with a value one
character too long and with a value carrying a control character. A column in neither register
fails the build until somebody decides which it is.

**It found three things on the day it was written, and none was findable by reading.**

- ``domain/vocabulary.py`` kept its own length check and had no character check at all,
  because `#1555` put that rule in ``text.fit`` and reached every other writer through
  ``text.require`` — which nothing there calls. A status label carrying a NUL was stored.
  That is `#1574`, fixed in the same commit as this file.
- A credential's title was measured in ``tokens.issue`` and written by
  ``authentication.issue_token``, one layer below. Only one caller existed, so nothing was
  wrong; a second would have gone round it, which is how the gap above arose. The check moved
  down to the function that stores the row.
- A tag's refusal names ``tags`` rather than ``name``, because that is the field a caller
  sends. Right, and now written down, so a refusal naming the wrong one would fail.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.calendars
import subroutine.domain.documents
import subroutine.domain.projects
import subroutine.domain.tags
import subroutine.domain.tasks
import subroutine.domain.text
import subroutine.domain.users
import subroutine.domain.vocabulary
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks

#: The fewest bounded columns the schema holds, as a floor under the walk.
#:
#: Measured at 61 across 20 tables on 2026-08-29. A walk that reads nothing reports every entry
#: below as stale and nothing as unclassified, which is why this is here as well as those.
FEWEST_COLUMNS = 50


Driver = typing.Callable[[test_api_tasks.World, str], object]


def _a_project (world: test_api_tasks.World) -> subroutine.db.models.project.Project:
	"""Return a project in the world's workspace to hang work off."""

	return subroutine.domain.projects.create(
		session=world.session,
		workspace_id=world.workspace.id,
		key=f"p{uuid.uuid4().hex[:8]}",
		title="Somewhere to put it",
	)


def _principal (world: test_api_tasks.World) -> subroutine.domain.authentication.Principal:
	"""Return the principal the world's token resolves to."""

	return subroutine.domain.authentication.authenticate(world.session, world.secret)


def _token_title (world: test_api_tasks.World, value: str) -> object:
	"""Mint a credential whose title is ``value``."""

	return subroutine.domain.authentication.issue_token(
		world.session, user=world.user, title=value
	)


def _feed_title (world: test_api_tasks.World, value: str) -> object:
	"""Mint a calendar feed whose title is ``value``."""

	return subroutine.domain.calendars.create(
		world.session, _principal(world), workspace_id=world.workspace.id, title=value
	)


def _document_title (world: test_api_tasks.World, value: str) -> object:
	"""Write a document whose title is ``value``."""

	return subroutine.domain.documents.create(
		world.session, project=_a_project(world), title=value
	)


def _task_title (world: test_api_tasks.World, value: str) -> object:
	"""File a task whose title is ``value``."""

	return subroutine.domain.tasks.create(
		world.session, project=_a_project(world), title=value
	)


def _project_key (world: test_api_tasks.World, value: str) -> object:
	"""Make a project whose key is ``value``."""

	return subroutine.domain.projects.create(
		session=world.session, workspace_id=world.workspace.id, key=value, title="Fine"
	)


def _project_title (world: test_api_tasks.World, value: str) -> object:
	"""Make a project whose title is ``value``."""

	return subroutine.domain.projects.create(
		session=world.session,
		workspace_id=world.workspace.id,
		key=f"p{uuid.uuid4().hex[:8]}",
		title=value,
	)


def _status_key (world: test_api_tasks.World, value: str) -> object:
	"""Add a status whose key is ``value``."""

	return subroutine.domain.vocabulary.create_status(
		world.session,
		workspace_id=world.workspace.id,
		entity_type="task",
		key=value,
		label="Fine",
		category="todo",
	)


def _status_label (world: test_api_tasks.World, value: str) -> object:
	"""Add a status whose label is ``value``."""

	return subroutine.domain.vocabulary.create_status(
		world.session,
		workspace_id=world.workspace.id,
		entity_type="task",
		key=f"k{uuid.uuid4().hex[:8]}",
		label=value,
		category="todo",
	)


def _link_type (field: str) -> Driver:
	"""Return a driver that puts ``value`` into one of a link type's three written fields.

	Spelled out rather than unpacked from a map, because a keyword-only signature of mixed
	types cannot be reached with ``**dict[str, str]`` under mypy — recorded here because the
	shorter version type-checks locally against ``src`` alone and fails the gate, which runs
	over the tests too.
	"""

	def driver (world: test_api_tasks.World, value: str) -> object:
		"""Write ``value`` into the chosen field and leave the others valid."""

		return subroutine.domain.vocabulary.create_link_type(
			world.session,
			workspace_id=world.workspace.id,
			key=value if field == "key" else f"k{uuid.uuid4().hex[:8]}",
			title=value if field == "title" else "Relates to",
			inverse_title=value if field == "inverse_title" else "Related from",
			category="describing",
		)

	return driver


def _tag_name (world: test_api_tasks.World, value: str) -> object:
	"""Make a tag named ``value``."""

	return subroutine.domain.tags.ensure(
		world.session, workspace_id=world.workspace.id, names=[value]
	)


def _user (field: str) -> Driver:
	"""Return a driver that puts ``value`` into one of an account's three written fields."""

	def driver (world: test_api_tasks.World, value: str) -> object:
		"""Write ``value`` into the chosen field and leave the others valid."""

		return subroutine.domain.users.create(
			world.session,
			username=value if field == "username" else f"u{uuid.uuid4().hex[:8]}",
			email=value if field == "email" else None,
			display_name=value if field == "display_name" else None,
		)

	return driver


def _workspace (field: str) -> Driver:
	"""Return a driver that puts ``value`` into one of a workspace's two written fields."""

	def driver (world: test_api_tasks.World, value: str) -> object:
		"""Write ``value`` into the chosen field and leave the others valid."""

		return subroutine.domain.workspaces.create(
			world.session,
			slug=value if field == "slug" else f"w{uuid.uuid4().hex[:8]}",
			title=value if field == "title" else "Fine",
			owner=world.user,
		)

	return driver


#: The bounded columns somebody types into, and what writes each one.
#:
#: Keyed ``table.column`` so the walk over the schema can ask about a column by name, and
#: driven through the domain rather than over HTTP because the domain is where the refusal is
#: decided and every surface inherits it — the defect this found was a domain writer that had
#: its own check instead.
DRIVEN: dict[str, tuple[Driver, str]] = {
	"api_token.title": (_token_title, "title"),
	"calendar_feed.title": (_feed_title, "title"),
	"document.title": (_document_title, "title"),
	"link_type.inverse_title": (_link_type("inverse_title"), "inverse_title"),
	"link_type.key": (_link_type("key"), "key"),
	"link_type.title": (_link_type("title"), "title"),
	"project.key": (_project_key, "key"),
	"project.title": (_project_title, "title"),
	"status.key": (_status_key, "key"),
	"status.label": (_status_label, "label"),
	# **The caller's word, not the column's.** A tag arrives in a list called ``tags`` and the
	# refusal names that, which is right: a message about ``name`` would be about a row the
	# caller never mentioned. Pinning it here is what makes the difference deliberate.
	"tag.name": (_tag_name, "tags"),
	"task.title": (_task_title, "title"),
	"user.display_name": (_user("display_name"), "display_name"),
	"user.email": (_user("email"), "email"),
	"user.username": (_user("username"), "username"),
	"workspace.slug": (_workspace("slug"), "slug"),
	"workspace.title": (_workspace("title"), "title"),
}


#: The bounded columns nobody types into, and why each is not driven above.
#:
#: An entry goes away when the column does, or when a writer appears that takes the value from
#: a caller — at which point it belongs in ``DRIVEN`` and this excuse is hiding a check rather
#: than explaining one.
NOT_TYPED: dict[str, str] = {
	"api_token.token_hash": "minted here and never sent",
	"api_token.token_prefix": "minted here and never sent",
	"calendar_feed.audience": "one of a fixed set, refused by name",
	"calendar_feed.token_hash": "minted here and never sent",
	"calendar_feed.token_prefix": "minted here and never sent",
	"comment.entity_type": "a discriminator this code writes, from a fixed set",
	"document.path": "derived from the tree, and bounded by the depth limit instead",
	"event.action": "a discriminator this code writes, from a fixed set",
	"event.entity_type": "a discriminator this code writes, from a fixed set",
	"event.subject_b_type": "a discriminator this code writes, from a fixed set",
	"event.subject_type": "a discriminator this code writes, from a fixed set",
	"instance.name": "written once by init, from a flag the operator owns",
	"instance.timezone": "a zone name, checked against the zone database",
	"item_type.category": "one of a fixed set, refused by name",
	"item_type.entity_type": "a discriminator this code writes, from a fixed set",
	"item_type.key": "seeded; no writer takes one from a caller",
	"item_type.label": "seeded; no writer takes one from a caller",
	"link.source_type": "a discriminator this code writes, from a fixed set",
	"link.target_type": "a discriminator this code writes, from a fixed set",
	"link_type.category": "one of a fixed set, refused by name",
	"login_link.token_hash": "minted here and never sent",
	"login_link.token_prefix": "minted here and never sent",
	"mention.source_type": "a discriminator this code writes, from a fixed set",
	"mention.target_type": "a discriminator this code writes, from a fixed set",
	"project.path": "derived from the tree, and bounded by the depth limit instead",
	"project.template": "one of a fixed set, refused by name",
	"project.visibility": "one of a fixed set, refused by name",
	"role.key": "seeded; no writer takes one from a caller",
	"role.title": "seeded; no writer takes one from a caller",
	"status.category": "one of a fixed set, refused by name",
	"status.entity_type": "a discriminator this code writes, from a fixed set",
	"tag.name_normalized": "derived from the name driven above",
	"task.path": "derived from the tree, and bounded by the depth limit instead",
	"task.recurrence_anchor": "one of a fixed set, refused by name",
	"task.recurrence_trigger": "one of a fixed set, refused by name",
	"task.timezone": "a zone name, checked against the zone database",
	"user.email_normalized": "derived from the address driven above",
	"user.timezone": "a zone name, checked against the zone database",
	"user.username_normalized": "derived from the username driven above",
	"verification.commit_sha": "read from git, never from a caller",
	"verification.tree_hash": "computed here, never sent",
	"web_session.token_hash": "minted here and never sent",
	"web_session.token_prefix": "minted here and never sent",
	"workspace.timezone": "a zone name, checked against the zone database",
}


def _bounded () -> dict[str, sqlalchemy.Column[typing.Any]]:
	"""Return every bounded ``String`` column in the schema, keyed ``table.column``.

	Derived from the metadata rather than listed, which is the whole value: a column added
	tomorrow fails the build until somebody says which register it belongs in.
	"""

	found = {}

	for table in subroutine.db.base.Base.metadata.tables.values():
		for column in table.columns:
			if isinstance(column.type, sqlalchemy.String) and column.type.length:
				found[f"{table.name}.{column.name}"] = column

	return found


def test_every_bounded_column_is_driven_or_excused () -> None:
	"""A new bounded column is a decision, and this is where it gets made."""

	columns = _bounded()

	assert len(columns) >= FEWEST_COLUMNS, (
		f"only {len(columns)} bounded columns were found, which is fewer than the "
		f"{FEWEST_COLUMNS} the schema holds — the walk has stopped reading the metadata, and "
		f"everything below then passes by measuring nothing"
	)

	unclassified = sorted(set(columns) - set(DRIVEN) - set(NOT_TYPED))

	assert not unclassified, (
		"these bounded columns are neither driven nor excused, so nothing says whether a "
		f"caller can overflow them: {unclassified}"
	)

	both = sorted(set(DRIVEN) & set(NOT_TYPED))

	assert not both, f"these are both driven and excused, which cannot both be true: {both}"


def test_no_register_here_names_a_column_that_has_gone () -> None:
	"""An entry for a column that no longer exists is a decision about nothing."""

	columns = set(_bounded())

	stale = sorted((set(DRIVEN) | set(NOT_TYPED)) - columns)

	assert not stale, f"these are registered and are not columns any more: {stale}"


@pytest.mark.parametrize("where", sorted(DRIVEN))
def test_a_value_too_long_for_its_column_is_refused (
	where: str, session: sqlalchemy.orm.Session
) -> None:
	"""One character over what the column holds, so the refusal is the column's own width.

	**Built from the column rather than from a constant**, which is what makes this a
	comparison of the two declarations instead of a second copy of one of them. A limit that
	drifted below its column would still refuse and pass; one that drifted above it would
	reach the database, where PostgreSQL raises and SQLite may not.
	"""

	world = test_api_tasks._world(session)
	column = _bounded()[where]
	driver, named = DRIVEN[where]
	length = typing.cast(int, typing.cast(sqlalchemy.String, column.type).length)

	with pytest.raises(subroutine.errors.SubroutineError) as refusal:
		driver(world, "a" * (length + 1))

	assert named in str(refusal.value.errors) or named in str(refusal.value), (
		f"{where} refused a value {length + 1} long without naming {named!r}: {refusal.value}"
	)


@pytest.mark.parametrize("where", sorted(DRIVEN))
def test_a_control_character_never_reaches_a_column (
	where: str, session: sqlalchemy.orm.Session
) -> None:
	"""`#1555`'s class, asked of every column rather than of the writers that had the fix.

	**A NUL is what decides it**: PostgreSQL refuses one outright and SQLite stores it, so a
	column that accepts one holds a row that cannot be copied between them — which is the
	failure `#1555` was filed for, reported naming no table, column or row.

	**Refusing and removing are both right answers and this asks for neither in particular.**
	A title is refused, because silently altering somebody's words is the truncation this
	project argues against; a workspace's short name is *shaped* by a normaliser that keeps
	only letters, numbers and hyphens, so the character is gone before any check sees it. What
	must not happen is that it is stored, and that is what is asserted.
	"""

	world = test_api_tasks._world(session)
	column = _bounded()[where]
	driver, _named = DRIVEN[where]

	try:
		driver(world, "probe\x00value")
		world.session.flush()

	except subroutine.errors.SubroutineError:
		return

	except sqlalchemy.exc.DBAPIError as refused:
		pytest.fail(
			f"{where}: the database turned this down and the domain did not, which is the "
			f"divergence itself — on SQLite it would have been stored: {refused}"
		)

	carrying = [
		one
		for one in world.session.scalars(sqlalchemy.select(column))
		if one and any(character in subroutine.domain.text.CONTROL_CHARACTERS for character in one)
	]

	assert not carrying, f"{where} stored a control character: {carrying!r}"
