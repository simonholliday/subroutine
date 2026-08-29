"""Every text column a caller can write refuses what a database would — ``#1571``, ``#1584``.

**Two declarations of one width, in modules that cannot see each other.** A column says how
much it will hold; a domain module says what it will accept. They agreed on the day each was
written and nothing compares them, which is this codebase's signature defect — and the failure
is not an exception. PostgreSQL raises where SQLite truncates or stores, so a row written on a
laptop is one production refuses, and ``db copy`` reports it naming no table, column or row.

**The register is the point, not the loop.** Of the text columns in the schema, most are ours
— a token hash, a discriminator, a derived path — and want a written reason rather than a
driver. The rest are somebody's typing, and each is driven with a value carrying a control
character, and with a value one character too long where the column has a width to overflow. A
column in no register fails the build until somebody decides which it is.

**Two rules over two populations, and conflating them is `#1584`.** *Does this fit* can only
be asked of a sized column; *is this text* is true of every column PostgreSQL stores. The
first version derived one population — bounded ``String`` columns — and ran both rules over
it, so the second rule was narrower than the thing it is about. ``description`` and ``body``
are unbounded ``Text``, and a NUL in one was a 500 on PostgreSQL, stored on SQLite, and a
stranded ``db copy``: exactly the divergence the file was written to close, one column type
along, invisible because the guard's own name described the smaller set. **This file was
called ``test_bounded_columns.py`` and the name was part of it.**

**So a bounded column may not be registered as prose.** That is asserted below, because the
cheap way out of a build failure here is to move a column into the register with the weaker
rule, and it would look like classification.

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
import subroutine.domain.comments
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

#: The fewest text columns the schema holds, as a floor under the walk.
#:
#: Measured at 73 across 20 tables on 2026-08-29 — 61 with a declared width and 12 without. A
#: walk that reads nothing reports every entry below as stale and nothing as unclassified,
#: which is why this is here as well as those.
FEWEST_COLUMNS = 60


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


def _task_description (world: test_api_tasks.World, value: str) -> object:
	"""File a task whose description is ``value``."""

	return subroutine.domain.tasks.create(
		world.session, project=_a_project(world), title="Fine", description=value
	)


def _document_body (world: test_api_tasks.World, value: str) -> object:
	"""Write a document whose body is ``value``."""

	return subroutine.domain.documents.create(
		world.session, project=_a_project(world), title="Fine", body=value
	)


def _project_description (world: test_api_tasks.World, value: str) -> object:
	"""Make a project whose description is ``value``."""

	return subroutine.domain.projects.create(
		session=world.session,
		workspace_id=world.workspace.id,
		key=f"p{uuid.uuid4().hex[:8]}",
		title="Fine",
		description=value,
	)


def _workspace_description (world: test_api_tasks.World, value: str) -> object:
	"""Describe the world's own workspace as ``value``.

	The only one of these driven through ``update`` rather than ``create``, because
	:func:`subroutine.domain.workspaces.create` takes no description at all.
	"""

	return subroutine.domain.workspaces.update(
		world.session, world.workspace, description=value, actor=_principal(world)
	)


def _comment_body (world: test_api_tasks.World, value: str) -> object:
	"""Say ``value`` on a task."""

	task = subroutine.domain.tasks.create(
		world.session, project=_a_project(world), title="Fine"
	)
	world.session.flush()

	return subroutine.domain.comments.create(
		world.session,
		entity_type="task",
		entity_id=task.id,
		body=value,
		actor=_principal(world),
	)


def _repeat (written: str) -> Driver:
	"""Return a driver that files a task repeating by ``written`` with ``value`` in it.

	**Two columns from one field, which is why the phrase is a parameter.** A repeat written
	in words is kept verbatim in ``recurrence_text`` and compiled into ``recurrence_rule``; a
	repeat written as an ``RRULE`` is stored as itself and keeps no text. So the same caller
	field reaches a different column depending on which it looks like, and each wants driving.
	"""

	def driver (world: test_api_tasks.World, value: str) -> object:
		"""File a task whose repeat is ``written`` with ``value`` spliced into it."""

		return subroutine.domain.tasks.create(
			world.session,
			project=_a_project(world),
			title="Fine",
			recurrence=written.format(value=value),
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


#: The unbounded text columns somebody types into, and what writes each one — `#1584`.
#:
#: **Prose, so there is no width to overflow and only the character rule applies.** A cap here
#: would be a second behaviour nobody asked for: this project's own specification lives in the
#: instance as documents of eighty kilobytes, so a limit chosen to reach the character check
#: would refuse our own records.
PROSE: dict[str, tuple[Driver, str]] = {
	"comment.body": (_comment_body, "body"),
	"document.body": (_document_body, "body"),
	"project.description": (_project_description, "description"),
	# **Driven rather than excused, though the grammar is what refuses.** Both are written from
	# one caller field: `every day` is kept verbatim and compiled, an `RRULE` is stored as
	# itself. Nothing here calls `text.readable` — a control character cannot survive the
	# parse — so this is what says the parse is still what stands between them and a column.
	"task.recurrence_rule": (_repeat("FREQ=DAILY{value}"), "repeat"),
	"task.recurrence_text": (_repeat("every day{value}"), "repeat"),
	"task.description": (_task_description, "description"),
	"workspace.description": (_workspace_description, "description"),
}


#: The unbounded text columns nobody types into, and why each is not driven above.
NOT_PROSE: dict[str, str] = {
	"role.description": "seeded; no writer takes one from a caller",
	"tag.description": "no writer takes one from a caller, on any surface",
	"user.password_hash": "a hash computed here; what somebody typed is never stored",
	"verification.output_excerpt": "captured from a command this code ran",
	"verification.summary": "written here from what a run produced",
}


def _textual () -> dict[str, sqlalchemy.Column[typing.Any]]:
	"""Return every text column in the schema, sized or not, keyed ``table.column``.

	Derived from the metadata rather than listed, which is the whole value: a column added
	tomorrow fails the build until somebody says which register it belongs in.

	``sqlalchemy.Text`` is a ``String`` with no length, so one test settles membership and
	:func:`_bounded` is this narrowed to the ones that have a width to overflow.
	"""

	found = {}

	for table in subroutine.db.base.Base.metadata.tables.values():
		for column in table.columns:
			if isinstance(column.type, sqlalchemy.String):
				found[f"{table.name}.{column.name}"] = column

	return found


def _bounded () -> dict[str, sqlalchemy.Column[typing.Any]]:
	"""Return every text column that declares a width, keyed ``table.column``."""

	return {
		where: column
		for where, column in _textual().items()
		if typing.cast(sqlalchemy.String, column.type).length
	}


#: The four registers, each with what a reader should call it in a failure.
REGISTERS = {
	"DRIVEN": set(DRIVEN),
	"NOT_TYPED": set(NOT_TYPED),
	"PROSE": set(PROSE),
	"NOT_PROSE": set(NOT_PROSE),
}


def test_every_text_column_is_driven_or_excused () -> None:
	"""A new text column is a decision, and this is where it gets made."""

	columns = _textual()

	assert len(columns) >= FEWEST_COLUMNS, (
		f"only {len(columns)} text columns were found, which is fewer than the "
		f"{FEWEST_COLUMNS} the schema holds — the walk has stopped reading the metadata, and "
		f"everything below then passes by measuring nothing"
	)

	registered = set().union(*REGISTERS.values())
	unclassified = sorted(set(columns) - registered)

	assert not unclassified, (
		"these text columns are in no register, so nothing says whether a caller can put "
		f"into them what a database will not hold: {unclassified}"
	)

	for name, entries in REGISTERS.items():
		for other, more in REGISTERS.items():
			if other <= name:
				continue

			both = sorted(entries & more)

			assert not both, f"these are in {name} and {other} at once: {both}"


def test_a_column_with_a_width_is_never_registered_as_prose () -> None:
	"""The escape from a build failure here must not be the register with the weaker rule.

	`#1584` was one population asked two questions, and the repair is two populations. The
	cheap way to satisfy that is to move a bounded column into ``PROSE``, which reads exactly
	like classification and quietly stops its width from being driven at all.
	"""

	bounded = set(_bounded())
	misfiled = sorted((set(PROSE) | set(NOT_PROSE)) & bounded)

	assert not misfiled, (
		"these declare a width and are registered as prose, so nothing checks that a caller "
		f"cannot overflow them: {misfiled}"
	)


def test_no_register_here_names_a_column_that_has_gone () -> None:
	"""An entry for a column that no longer exists is a decision about nothing."""

	columns = set(_textual())

	stale = sorted(set().union(*REGISTERS.values()) - columns)

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


@pytest.mark.parametrize("where", sorted({**DRIVEN, **PROSE}))
def test_a_control_character_never_reaches_a_column (
	where: str, session: sqlalchemy.orm.Session
) -> None:
	"""`#1555`'s class, asked of every column rather than of the writers that had the fix.

	**Over every text column a caller writes, sized or not, which is `#1584`.** This ran over
	the bounded ones alone, because it was written beside the length rule and inherited its
	population — and a NUL is refused by PostgreSQL in a ``Text`` column exactly as in a
	``String(128)``, so a task's description was the same divergence with nothing looking at
	it.

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
	column = _textual()[where]
	driver, _named = {**DRIVEN, **PROSE}[where]

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


#: Editing a piece of prose, for the four columns that are written twice.
#:
#: **Keyed by column so a reader can line them up with :data:`PROSE`**, whose drivers all
#: create. Splitting them like this rather than letting a register hold two drivers keeps the
#: population check above about columns, which is what makes it derivable from the schema.
EDITED: dict[str, typing.Callable[[test_api_tasks.World, str], object]] = {
	"document.body": lambda world, value: subroutine.domain.documents.update(
		world.session,
		subroutine.domain.documents.create(
			world.session, project=_a_project(world), title="Fine"
		),
		body=value,
		actor=_principal(world),
	),
	"project.description": lambda world, value: subroutine.domain.projects.update(
		world.session,
		_a_project(world),
		description=value,
		actor=_principal(world),
	),
	"task.description": lambda world, value: subroutine.domain.tasks.update(
		world.session,
		subroutine.domain.tasks.create(
			world.session, project=_a_project(world), title="Fine"
		),
		description=value,
		actor=_principal(world),
	),
	"workspace.description": lambda world, value: subroutine.domain.workspaces.update(
		world.session, world.workspace, description=value, actor=_principal(world)
	),
}


@pytest.mark.parametrize("where", sorted(EDITED))
def test_a_control_character_is_refused_when_prose_is_edited_too (
	where: str, session: sqlalchemy.orm.Session
) -> None:
	"""The second writer, which a guard keyed by column cannot see.

	**This file's own first finding, one register along** (`#1571`): a credential's title was
	checked one layer above the function that stored the row, and nothing was broken only
	because there was one caller. Every column in :data:`PROSE` but the comment's body has two
	— it is written when the item is filed and again when it is edited — and the walk above
	drives whichever one its driver happens to use.

	So this is not derived from the schema and cannot be: it is a claim about writers, and the
	set of writers is not something the metadata knows. What it buys is that the create-side
	check being present says nothing about the edit-side one, which is exactly how `#1574`
	happened.
	"""

	world = test_api_tasks._world(session)

	with pytest.raises(subroutine.errors.SubroutineError):
		EDITED[where](world, "probe\x00value")
		world.session.flush()
