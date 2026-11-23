"""Two requests racing to one unique key - `SR#5159`.

A bullet of L-7 of the cold review of 2026-10-17, decided in `#5097`: nothing caught a unique
violation, so the second of two requests making one new tag at once, or two projects with one key,
was answered 500 over HTTP, *could not be read* at the terminal and SQLAlchemy's own text in an
agent's tool - measured on both backends. **The tag's second request now succeeds with the first's
tag, and every other race answers 409 ``duplicate_key``, as the later request would have been
answered a moment later.** Any other integrity fault is still a defect, and still reported as one.
"""

import threading
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.event
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.failures
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.session
import subroutine.domain.projects
import subroutine.domain.tags
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.mcp.protocol
import test_api_tasks
import test_migrations

#: A migrated database of its own on each backend, bound here so pytest finds it as a fixture.
migrated_url = test_migrations.migrated_url

#: What a race to a unique key is answered with, on every surface, as approved.
SAID = "Somebody else made one by the same name while yours was being written."
HINT = "Nothing was changed. Look at what is there now, then use it or choose another name."

#: A tool the refusal can be attributed to, as `test_request_timeout` builds one: any serves.
_ANY_TOOL = subroutine.mcp.protocol.Tool(
	name="subroutine_probe",
	title="Probe",
	description="Stand in for whichever tool was being called.",
	schema={"type": "object", "properties": {}},
	call=lambda arguments: "",
)


def _workspace (url: str) -> uuid.UUID:
	"""Make an account and a workspace in the database at ``url``, committed, and return its id."""

	engine = subroutine.db.session.create_engine(url)

	try:
		with sqlalchemy.orm.Session(engine, expire_on_commit=False) as setup:
			founder = subroutine.domain.users.create(setup, username="trinity")
			workspace = subroutine.domain.workspaces.create(
				setup, slug="metacortex", title="Metacortex", owner=founder
			)
			setup.commit()

			return workspace.id

	finally:
		engine.dispose()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_two_requests_making_one_new_tag_at_once_both_have_it (migrated_url: str) -> None:
	"""Each had read that the tag was not there, so the second failed on the unique index.

	**Raced for real**, on two connections: the second is let go only once it is about to write
	the tag, so it has already read that the tag is missing, and the first then commits. Whether
	the second is waiting on the first or arrives just after, it meets the first's tag.
	"""

	workspace_id = _workspace(migrated_url)
	engine = subroutine.db.session.create_engine(migrated_url)
	racing_engine = subroutine.db.session.create_engine(migrated_url)
	about_to_write = threading.Event()

	def noticed (
		connection: typing.Any,
		cursor: typing.Any,
		statement: str,
		parameters: typing.Any,
		context: typing.Any,
		executemany: bool,
	) -> None:
		"""Say when the second request is about to write a tag."""

		if statement.lstrip().upper().startswith("INSERT INTO TAG "):
			about_to_write.set()

	sqlalchemy.event.listen(racing_engine, "before_cursor_execute", noticed)
	first = sqlalchemy.orm.Session(engine, expire_on_commit=False)
	second = sqlalchemy.orm.Session(racing_engine, expire_on_commit=False)
	got: dict[str, typing.Any] = {}

	def racing () -> None:
		"""Ask for the same tag, spelled another way, while the first holds its own."""

		try:
			made = subroutine.domain.tags.ensure(
				second, workspace_id=workspace_id, names=["Errands"]
			)
			second.commit()
			got["tags"] = [tag.id for tag in made]

		except Exception as error:
			got["error"] = error

	try:
		made = subroutine.domain.tags.ensure(first, workspace_id=workspace_id, names=["errands"])
		thread = threading.Thread(target=racing)
		thread.start()

		assert about_to_write.wait(30), "the second never reached its write, so nothing raced"

		first.commit()
		thread.join(30)

		assert not thread.is_alive(), "the second request never finished"
		assert "error" not in got, repr(got.get("error"))
		assert got["tags"] == [made[0].id], "the second made a tag of its own"

		with sqlalchemy.orm.Session(engine) as reading:
			model = subroutine.db.models.vocabulary.Tag
			names = reading.scalars(
				sqlalchemy.select(model.name).where(model.workspace_id == workspace_id)
			).all()

		assert names == ["errands"], names

	finally:
		first.close()
		second.close()
		engine.dispose()
		racing_engine.dispose()


def _a_real_violation (url: str, *, of: str) -> sqlalchemy.exc.IntegrityError:
	"""Return what the database at ``url`` raised for a second row repeating a unique key, or for
	a row missing a value it must have - ``of`` is ``"unique"`` or ``"not null"``.

	**Raised by the database itself**, as `test_request_timeout` raises a real busy one: what this
	reads is where each driver keeps its state or its name, which a stand-in could only assert.
	"""

	metadata = sqlalchemy.MetaData()
	table = sqlalchemy.Table(
		"probe_5159",
		metadata,
		sqlalchemy.Column("n", sqlalchemy.Integer, primary_key=True),
		sqlalchemy.Column("key", sqlalchemy.String(16), nullable=False, unique=True),
	)
	engine = subroutine.db.session.create_engine(url)

	try:
		metadata.create_all(engine)

		with engine.begin() as connection:
			connection.execute(table.insert().values(n=1, key="web"))

		try:
			with engine.begin() as connection:
				if of == "unique":
					connection.execute(table.insert().values(n=2, key="web"))

				else:
					connection.execute(table.insert().values(n=2, key=None))

		except sqlalchemy.exc.IntegrityError as raised:
			return raised

		raise AssertionError("the database took the row, so this proves nothing")

	finally:
		metadata.drop_all(engine)
		engine.dispose()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_a_key_taken_meanwhile_is_told_from_what_each_backend_called_it (migrated_url: str) -> None:
	"""PostgreSQL says it in a state and SQLite in an error name; both are asked."""

	answer = subroutine.db.failures.duplicated(_a_real_violation(migrated_url, of="unique"))

	assert answer is not None, "a real unique violation was not recognised"
	assert answer.CODE == "duplicate_key", answer.CODE
	assert (answer.detail, answer.hint) == (SAID, HINT)


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_any_other_integrity_fault_is_still_a_defect (migrated_url: str) -> None:
	"""A missing value is not a race, so it is not said to be one."""

	assert subroutine.db.failures.duplicated(_a_real_violation(migrated_url, of="not null")) is None


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_the_terminal_says_it_rather_than_that_the_database_could_not_be_read (
	migrated_url: str,
) -> None:
	"""The local client called every database error it did not know *could not be read*, under
	advice to check the database's address, about a database that had answered."""

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True, database_url=migrated_url),
		session_factory=None,
	)
	violation = _a_real_violation(migrated_url, of="unique")

	with pytest.raises(subroutine.errors.SubroutineError) as refused, client._reported():
		raise violation

	assert "could not be read" not in refused.value.detail, refused.value.detail
	assert refused.value.CODE == "duplicate_key", refused.value.CODE
	assert (refused.value.detail, refused.value.hint) == (SAID, HINT)


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_an_agent_is_told_it_rather_than_shown_the_sql (migrated_url: str) -> None:
	"""The tools fell through to ``str(failure)``: the statement, the index and the values bound."""

	answer = subroutine.mcp.protocol._explained(
		_a_real_violation(migrated_url, of="unique"), _ANY_TOOL
	)

	assert answer.splitlines()[:2] == [SAID, HINT], answer
	assert "INSERT" not in answer and "probe_5159" not in answer, answer
	assert "'web'" not in answer and "sqlalche" not in answer, answer


def test_two_projects_made_with_one_key_at_once_answer_409 (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Over HTTP, on both backends: the second's check had passed before the first's row landed.

	**The check is made to pass, so the second meets the real index**, which is where a race
	leaves it: what this asserts is what the request is answered once it gets there.
	"""

	world = test_api_tasks._world(session)
	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	monkeypatch.setattr(
		subroutine.domain.projects, "_refuse_duplicate_key", lambda *given, **named: None
	)

	refused = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website again"})

	assert refused.status_code == 409, refused.text
	assert refused.json()["code"] == "duplicate_key", refused.text
	assert (refused.json()["detail"], refused.json()["hint"]) == (SAID, HINT)
	assert "uq_" not in refused.text, "the index was named"


#: What a race to a key holding no name is answered with (`SR#5321`, decision `#5285`).
NEUTRAL = "Somebody else made the same change while yours was being written."
NEUTRAL_HINT = "Nothing was changed. Look at what is there now: it may already be as you asked."


def _a_task_and_a_tag (url: str, workspace_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
	"""Make a task in the workspace's Inbox, and a tag, committed, and return their ids."""

	engine = subroutine.db.session.create_engine(url)

	try:
		with sqlalchemy.orm.Session(engine, expire_on_commit=False) as setup:
			model = subroutine.db.models.project.Project
			inbox = setup.scalars(
				sqlalchemy.select(model).where(
					model.workspace_id == workspace_id, model.is_inbox.is_(True)
				)
			).one()
			task = subroutine.domain.tasks.create(setup, project=inbox, title="Book the venue")
			[tag] = subroutine.domain.tags.ensure(
				setup, workspace_id=workspace_id, names=["errands"]
			)
			setup.commit()

			return task.id, tag.id

	finally:
		engine.dispose()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_two_requests_adding_one_tag_to_one_task_at_once_both_succeed (migrated_url: str) -> None:
	"""`SR#5321`, decision `#5285`: the second failed its whole request on the join's key.

	**Raced for real**, as the tag's own race is: the second is let go only once it is about to
	write the join row, so it has read that the task does not carry the tag, and the first then
	commits. Both succeed, and the task carries it once.
	"""

	workspace_id = _workspace(migrated_url)
	task_id, tag_id = _a_task_and_a_tag(migrated_url, workspace_id)
	engine = subroutine.db.session.create_engine(migrated_url)
	racing_engine = subroutine.db.session.create_engine(migrated_url)
	about_to_write = threading.Event()

	def noticed (
		connection: typing.Any,
		cursor: typing.Any,
		statement: str,
		parameters: typing.Any,
		context: typing.Any,
		executemany: bool,
	) -> None:
		"""Say when the second request is about to write the join row."""

		if statement.lstrip().upper().startswith("INSERT INTO TASK_TAG"):
			about_to_write.set()

	sqlalchemy.event.listen(racing_engine, "before_cursor_execute", noticed)
	first = sqlalchemy.orm.Session(engine, expire_on_commit=False)
	second = sqlalchemy.orm.Session(racing_engine, expire_on_commit=False)
	task_model = subroutine.db.models.work.Task
	tag_model = subroutine.db.models.vocabulary.Tag
	got: dict[str, typing.Any] = {}

	def racing () -> None:
		"""Add the same tag to the same task while the first holds its own row."""

		try:
			task, tag = second.get(task_model, task_id), second.get(tag_model, tag_id)

			assert task is not None and tag is not None

			subroutine.domain.tags.apply_to(second, task, [tag])
			second.commit()
			got["done"] = True

		except Exception as error:
			got["error"] = error

	try:
		task, tag = first.get(task_model, task_id), first.get(tag_model, tag_id)

		assert task is not None and tag is not None

		subroutine.domain.tags.apply_to(first, task, [tag])
		thread = threading.Thread(target=racing)
		thread.start()

		assert about_to_write.wait(30), "the second never reached its write, so nothing raced"

		first.commit()
		thread.join(30)

		assert not thread.is_alive(), "the second request never finished"
		assert "error" not in got, repr(got.get("error"))

		with sqlalchemy.orm.Session(engine) as reading:
			join = subroutine.db.models.work.TaskTag
			carried = reading.scalars(sqlalchemy.select(join.tag_id).where(join.task_id == task_id))

			assert list(carried) == [tag_id]

	finally:
		first.close()
		second.close()
		engine.dispose()
		racing_engine.dispose()


def test_tags_and_their_rows_are_written_in_one_order_whatever_was_asked (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#5321`, L-2 of the cold review of 2026-11-11: ``a, b`` beside ``b, a`` could deadlock.

	PostgreSQL takes each row's index entry in turn, so a request holding ``b`` and waiting on
	``a`` deadlocks one holding ``a`` and waiting on ``b``. **Both write in one order**, so the
	later waits on the first's first row and holds nothing the first needs. What is returned still
	follows the order asked for. Asked of what is sent, since when two writers meet is the
	database's to decide.
	"""

	founder = subroutine.domain.users.create(session, username=f"trinity-{uuid.uuid4().hex[:6]}")
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"metacortex-{uuid.uuid4().hex[:6]}", title="Metacortex", owner=founder
	)
	model = subroutine.db.models.project.Project
	inbox = session.scalars(
		sqlalchemy.select(model).where(model.workspace_id == workspace.id, model.is_inbox.is_(True))
	).one()
	task = subroutine.domain.tasks.create(session, project=inbox, title="Book the venue")
	sent: list[list[dict[str, typing.Any]]] = []
	execute = session.execute

	def recording (statement: typing.Any, *given: typing.Any, **named: typing.Any) -> typing.Any:
		"""Keep each list of rows handed to the database, then hand it on."""

		if given and isinstance(given[0], list):
			sent.append(given[0])

		return execute(statement, *given, **named)

	monkeypatch.setattr(session, "execute", recording)

	made = subroutine.domain.tags.ensure(
		session, workspace_id=workspace.id, names=["web", "errands"]
	)
	subroutine.domain.tags.apply_to(session, task, made)

	assert [tag.name for tag in made] == ["web", "errands"], "the order asked for was lost"
	assert [row["name_normalized"] for row in sent[0]] == ["errands", "web"], sent
	assert [row["tag_id"] for row in sent[1]] == sorted(tag.id for tag in made), sent


def test_two_shares_of_one_project_with_one_person_at_once_answer_409_with_no_name (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#5321`, decision `#5285`: a race to a key holding no name was told a name clashed.

	**The check is made to pass, so the second meets the real key**, as the project's race above:
	a share has no name, so it is answered that somebody made the same change, with no *choose
	another name*. A race to a project's key keeps that sentence, above.
	"""

	world = test_api_tasks._world(session)
	made = world.call(
		"POST",
		"/v1/projects",
		json={"key": "secret", "title": "Redundancies", "visibility": "private"},
	)

	assert made.status_code == 201, made.text

	pat = subroutine.domain.users.create(session, username=f"pat-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, pat, role_key="member")
	session.flush()
	shared = world.call("POST", "/v1/projects/secret/members", json={"username": pat.username})

	assert shared.status_code in (200, 201), shared.text

	monkeypatch.setattr(subroutine.domain.projects, "_membership_of", lambda *given, **named: None)

	refused = world.call("POST", "/v1/projects/secret/members", json={"username": pat.username})

	assert refused.status_code == 409, refused.text
	assert refused.json()["code"] == "duplicate_key", refused.text
	assert (refused.json()["detail"], refused.json()["hint"]) == (NEUTRAL, NEUTRAL_HINT)
	assert "project_member" not in refused.text, "the key was named"


class _NamedOnlyByIndex(Exception):
	"""What SQLite raises for a key an index holds by its name alone: an expression's, a part's."""

	sqlite_errorname = "SQLITE_CONSTRAINT_UNIQUE"


def test_a_key_whose_columns_cannot_be_read_keeps_the_sentence_every_race_had () -> None:
	"""`SR#5321`: what cannot be read is not said to hold no name.

	SQLite names a partial or an expression index only by its name, *UNIQUE constraint failed:
	index '...'*, so which columns it holds cannot be read. **It keeps the sentence every race said
	before**, since nothing established that no name was in it.
	"""

	raised = sqlalchemy.exc.IntegrityError(
		"INSERT INTO project ...", {}, _NamedOnlyByIndex("UNIQUE constraint failed: index 'uq_x'")
	)
	answer = subroutine.db.failures.duplicated(raised)

	assert answer is not None, "a unique violation was not recognised"
	assert (answer.detail, answer.hint) == (SAID, HINT)
