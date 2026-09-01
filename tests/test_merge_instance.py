"""Tests for the one-off that folds a self-installed instance into a shared one.

`SR#1756`. This is not a feature and will be run a handful of times under supervision — which
is the argument for testing it rather than against. A script somebody runs once, on real data
they cannot get back, is exactly the code where a rehearsal has to prove something.

**The end-to-end tests build two real instances and merge one into the other**, because every
interesting property here is about two databases disagreeing and no unit test can hold that.
"""

import pathlib
import sys
import typing
import uuid

import pytest
import sqlalchemy

import subroutine.db.base
import subroutine.db.migrate
import subroutine.db.models
import subroutine.db.seed
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.workspaces

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

# After the path is set, which is `tests/test_deep_clean.py`'s arrangement — `scripts/` is
# not a package, and making it one would put the gate's own tooling on the import path of
# everything that installs this.
import merge_instance


def _instance (path: pathlib.Path, slug: str, username: str) -> str:
	"""Build a migrated instance with one workspace, one account and a seeded vocabulary."""

	url = f"sqlite:///{path}"

	subroutine.db.migrate.upgrade(url)

	engine = subroutine.db.session.create_engine(url)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			# **Through the domain rather than by inserting rows**, because a workspace made by
			# hand has no Inbox — `SR#301` moved that into `workspaces.create`, and a fixture
			# that skips it cannot file anything.
			owner = subroutine.db.models.identity.User(
				username=username, username_normalized=username
			)

			session.add(owner)
			session.flush()
			subroutine.domain.workspaces.create(
				session, slug=slug, title=slug.title(), owner=owner
			)
			session.commit()

	finally:
		engine.dispose()

	return url


def _add (
	url: str, *, title: str, ref: int, project: str = "inbox", description: str | None = None
) -> uuid.UUID:
	"""Write one task straight into an instance, with a ref of our choosing."""

	engine = subroutine.db.session.create_engine(url)
	identifier = subroutine.db.types.new_uuid()

	try:
		with engine.begin() as connection:
			tables = subroutine.db.base.Base.metadata.tables
			workspace = connection.execute(
				sqlalchemy.select(tables["workspace"].c.id)
			).scalar_one()
			user = connection.execute(sqlalchemy.select(tables["user"].c.id)).scalar_one()
			filed = connection.execute(
				sqlalchemy.select(tables["project"].c.id).where(
					tables["project"].c.key == project
				)
			).scalar_one()
			status = connection.execute(
				sqlalchemy.select(tables["status"].c.id).where(
					tables["status"].c.entity_type == "task", tables["status"].c.key == "open"
				)
			).scalar_one()
			kind = connection.execute(
				sqlalchemy.select(tables["item_type"].c.id).where(
					tables["item_type"].c.entity_type == "task",
					tables["item_type"].c.key == "task",
				)
			).scalar_one()

			connection.execute(
				sqlalchemy.insert(tables["task"]).values(
					id=identifier, workspace_id=workspace, project_id=filed, status_id=status,
					type_id=kind, ref=ref, title=title, description=description,
					path=f"/{identifier}/", depth=0, created_by=user,
				)
			)
			connection.execute(
				sqlalchemy.update(tables["workspace"]).values(next_ref_number=ref + 1)
			)

	finally:
		engine.dispose()

	return identifier


def _project (url: str, key: str) -> None:
	"""Add a project to an instance."""

	engine = subroutine.db.session.create_engine(url)

	try:
		with engine.begin() as connection:
			tables = subroutine.db.base.Base.metadata.tables
			workspace = connection.execute(
				sqlalchemy.select(tables["workspace"].c.id)
			).scalar_one()
			status = connection.execute(
				sqlalchemy.select(tables["status"].c.id).where(
					tables["status"].c.entity_type == "project"
				)
			).scalars().first()
			identifier = subroutine.db.types.new_uuid()

			connection.execute(
				sqlalchemy.insert(tables["project"]).values(
					id=identifier, workspace_id=workspace, key=key, title=key,
					status_id=status, path=f"/{identifier}/", depth=0, visibility="public",
				)
			)

	finally:
		engine.dispose()


@pytest.fixture
def two (tmp_path: pathlib.Path) -> tuple[str, str]:
	"""A source holding two items and a target holding one, both ready to merge."""

	source = _instance(tmp_path / "theirs.db", "theirs", "oli")
	target = _instance(tmp_path / "ours.db", "ours", "oli")

	_add(source, title="Set up the build", ref=1)
	_add(source, title="Cache the roster", ref=2, description="Blocked on #1, and #900 is not.")
	_add(target, title="Simon's own", ref=1)

	return source, target


def _rows (url: str, statement: sqlalchemy.Select[typing.Any]) -> list[typing.Any]:
	"""Read from an instance and close the engine."""

	engine = subroutine.db.session.create_engine(url)

	try:
		with engine.connect() as connection:
			return list(connection.execute(statement))

	finally:
		engine.dispose()


def test_every_table_is_either_carried_or_refused_in_writing () -> None:
	"""**The ratchet, and the only test here that matters after today.**

	A table added to the schema is silently *not* merged unless somebody notices, and nobody
	reviews a one-off script when they add a column. So the population is derived from the
	metadata and a table in neither list fails the build — which forces the decision to be
	written down rather than defaulted.
	"""

	merge_instance._every_table_is_accounted_for()

	overlap = set(merge_instance.CARRIED) & set(merge_instance.NOT_CARRIED)

	assert not overlap, f"{sorted(overlap)} is both carried and refused"

	assert all(merge_instance.NOT_CARRIED.values()), (
		"a table is refused with an empty reason, which is a decision nobody can check"
	)


def test_a_merged_item_keeps_its_moment_its_author_and_its_id (
	two: tuple[str, str],
) -> None:
	"""The three things nothing above the domain can carry.

	``created_at`` is on neither request model, so an import through the API or the CLI stamps
	every row with the moment it ran and attributes it to whoever held the token. That is the
	whole reason this works below the domain, and it is the property most worth proving.

	**The id matters as much as the other two**, because a link, a mention, a parent and a
	recurrence template all reference one — so an id that survives is what makes every join
	survive without being rebuilt.
	"""

	source, target = two
	tasks = subroutine.db.base.Base.metadata.tables["task"]
	users = subroutine.db.base.Base.metadata.tables["user"]

	before = {
		row.id: (row.ref, row.created_at)
		for row in _rows(source, sqlalchemy.select(tasks.c.id, tasks.c.ref, tasks.c.created_at))
	}

	merge_instance.merge(
		source, target, "ours", projects={}, users={}, commit=True,
	)

	after = {
		row.id: (row.ref, row.created_at, row.username)
		for row in _rows(
			target,
			sqlalchemy.select(tasks.c.id, tasks.c.ref, tasks.c.created_at, users.c.username)
			.join(users, users.c.id == tasks.c.created_by),
		)
	}

	for identifier, (ref, when) in before.items():
		assert identifier in after, "the id changed, so every link to it would have to be rebuilt"

		landed = after[identifier]

		assert landed[1] == when, f"#{ref} came back stamped {landed[1]} instead of {when}"
		assert landed[2] == "oli", f"#{ref} is attributed to {landed[2]}"


def test_a_ref_that_names_one_of_their_items_is_renumbered_and_nothing_else_is (
	two: tuple[str, str],
) -> None:
	"""§6.15's hazard: a number in somebody's prose may be an issue in another tracker.

	There is no way to tell the two apart except by asking whether it resolves here, so a `#N`
	naming one of their own items moves and every other one is left exactly as it was. Getting
	this wrong in the permissive direction silently points their writing at somebody else's
	work, which is the failure nothing downstream would ever report.
	"""

	source, target = two
	tasks = subroutine.db.base.Base.metadata.tables["task"]

	report, _maps, _landed = merge_instance.merge(
		source, target, "ours", projects={}, users={}, commit=True,
	)

	prose = _rows(
		target,
		sqlalchemy.select(tasks.c.description).where(tasks.c.title == "Cache the roster"),
	)[0][0]

	assert "#2" in prose, f"#1 should have become #2 here, and the text reads {prose!r}"
	assert "#900" in prose, "a number naming nothing here was rewritten to name something"
	assert report.left_alone == {900: 1}, (
		f"the report should say which numbers it left alone, and says {report.left_alone}"
	)


def test_the_target_keeps_its_own_numbering (two: tuple[str, str]) -> None:
	"""Their `#1` cannot land on the target's `#1`, and the counter has to move past both."""

	source, target = two
	tasks = subroutine.db.base.Base.metadata.tables["task"]
	workspace = subroutine.db.base.Base.metadata.tables["workspace"]

	merge_instance.merge(source, target, "ours", projects={}, users={}, commit=True)

	refs = sorted(row[0] for row in _rows(target, sqlalchemy.select(tasks.c.ref)))

	assert refs == [1, 2, 3], f"the refs collided or were compacted: {refs}"

	counter = _rows(target, sqlalchemy.select(workspace.c.next_ref_number))[0][0]

	assert counter == 4, f"the next item written would be #{counter}, which is already taken"


def test_an_account_with_nowhere_to_land_stops_the_run (tmp_path: pathlib.Path) -> None:
	"""**A row quietly reattributed says nothing about having been.**

	Attribution is the reason a person hands over work they would otherwise supervise
	(`SR#1414`), and `SR#1449` is what it costs when it is wrong: seven commits and 53
	backlinks recorded against the wrong principal, rendered perfectly from a field naming
	somebody else. So an account this cannot place is a refusal rather than a default.

	This one guards a *belief*: the run was designed on the understanding that Oli's instance
	has one account and no agent. Detection is what makes that safe to be wrong about.
	"""

	source = _instance(tmp_path / "theirs.db", "theirs", "oli")
	target = _instance(tmp_path / "ours.db", "ours", "simon")

	_add(source, title="Set up the build", ref=1)

	with pytest.raises(merge_instance.Refused) as refused:
		merge_instance.merge(source, target, "ours", projects={}, users={}, commit=True)

	assert "oli" in str(refused.value)

	# And it is a refusal rather than a crash: naming where it lands is enough to proceed.
	merge_instance.merge(
		source, target, "ours", projects={}, users={"oli": "simon"}, commit=True
	)


def test_a_project_holding_items_and_no_map_stops_the_run (tmp_path: pathlib.Path) -> None:
	"""Only projects that hold something have to map, so an empty one costs nobody a decision."""

	source = _instance(tmp_path / "theirs.db", "theirs", "oli")
	target = _instance(tmp_path / "ours.db", "ours", "oli")

	_project(source, "notes")
	_project(source, "never-used")
	_add(source, title="How the build works", ref=1, project="notes")

	with pytest.raises(merge_instance.Refused) as refused:
		merge_instance.merge(source, target, "ours", projects={}, users={}, commit=True)

	assert "notes" in str(refused.value)
	assert "never-used" not in str(refused.value), (
		"an empty project was demanded, so somebody would recreate one to satisfy a script"
	)

	_project(target, "oli-notes")
	merge_instance.merge(
		source, target, "ours", projects={"notes": "oli-notes"}, users={}, commit=True
	)


def test_a_word_the_target_has_not_agreed_stops_the_run (tmp_path: pathlib.Path) -> None:
	"""Vocabulary is matched by key and never invented.

	A status or a type is a word the workspace has agreed on (§5.5), so carrying one in changes
	what the target *means* — which is a decision for the people using it rather than a side
	effect of an import. A tag is the opposite and is carried, because a tag is a word somebody
	typed rather than a term anybody agreed.
	"""

	source = _instance(tmp_path / "theirs.db", "theirs", "oli")
	target = _instance(tmp_path / "ours.db", "ours", "oli")
	statuses = subroutine.db.base.Base.metadata.tables["status"]

	engine = subroutine.db.session.create_engine(source)

	try:
		with engine.begin() as connection:
			workspace = connection.execute(
				sqlalchemy.select(subroutine.db.base.Base.metadata.tables["workspace"].c.id)
			).scalar_one()

			connection.execute(
				sqlalchemy.insert(statuses).values(
					id=subroutine.db.types.new_uuid(), workspace_id=workspace,
					entity_type="task", key="parked", label="Parked", category="todo",
					position=9000,
				)
			)

	finally:
		engine.dispose()

	with pytest.raises(merge_instance.Refused) as refused:
		merge_instance.merge(source, target, "ours", projects={}, users={}, commit=True)

	assert "parked" in str(refused.value)


def test_merging_the_same_instance_twice_says_so_rather_than_failing_on_a_key (
	two: tuple[str, str],
) -> None:
	"""Running it twice is safe and was illegible, which is not the same thing.

	Driven: the second run fails on a duplicate primary key and the transaction rolls back, so
	nothing is lost. What arrives is several screens of SQL holding every column of every row,
	met by somebody who is not sure whether the first run worked — which is precisely the
	question this now answers in a sentence.
	"""

	source, target = two

	merge_instance.merge(source, target, "ours", projects={}, users={}, commit=True)

	with pytest.raises(merge_instance.Refused) as refused:
		merge_instance.merge(source, target, "ours", projects={}, users={}, commit=True)

	assert "merged before" in str(refused.value)


def test_two_databases_at_different_schemas_are_not_merged (two: tuple[str, str]) -> None:
	"""A column on one side and not the other would be dropped without anybody being told."""

	source, target = two

	subroutine.db.migrate.downgrade(source, "9c41d0b7ae52")

	with pytest.raises(merge_instance.Refused) as refused:
		merge_instance.merge(source, target, "ours", projects={}, users={}, commit=True)

	assert "db upgrade" in str(refused.value), (
		f"the refusal should name the remedy, and says {refused.value}"
	)
