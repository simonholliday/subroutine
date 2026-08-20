"""A field a surface *accepts* is proved to reach what stores it — `#919`.

``tests/test_reach.py`` answers three questions and all three read **declarations**: does a
client call the route (`#141`), does it pass every field the body accepts (`#427`), does it pass
every filter the query accepts (`#501`). None asks whether the value survives the journey, so a
signature widened without its body is invisible to every one of them — and that shipped three
times in one arc:

* ``starts`` and ``snooze`` were accepted by both clients' ``update`` after `#854` and dropped
  before the wire: the signature grew and the body dict did not.
* ``PATCH /v1/tasks`` declared ``recurrence`` and the handler never forwarded it. **200, having
  changed nothing.**
* `#918` — ``recurrence_anchor`` was accepted by ``create`` and ``update`` in the domain itself
  and discarded unless ``recurrence`` arrived beside it.

All three were live, and none was found by the suite: each surfaced because somebody happened to
widen the same method again for something else.

**So the question here is behavioural rather than structural.** Give a field a value, read the
entity back, give it a *different* value, read it back again — and fail if the two readings are
the same. The pair is what makes it worth anything: setting one value and finding it there can
be a coincidence of what the fixture already held, and a field that is accepted and dropped is
invisible to that reading.

**The cases derive from the request models**, so a field added tomorrow is a case tomorrow. What
a field has to be *given* cannot be derived — every one of a dozen ``str | None`` fields means
something different, a status key or a timezone or a date expression — so that is written down
in :data:`CHANGES`, and a field with neither an entry there nor one in :data:`NOT_STORED` fails
by name. The population is derived; the exceptions are written; neither can fall behind quietly.
"""

import datetime
import inspect
import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks
import test_api_writability

#: Two values for one field, set in turn. Keyed by the name the *request* uses.
#:
#: **Both readings come from the same fixture item**, so each case is independent of every other
#: and of the order they run in. What is asserted is that the entity read back differs between
#: them — never that it holds any particular value, which would be a second copy of whatever the
#: service does with it (a date expression resolves, an estimate is parsed, a project name is
#: looked up) and would fail for reasons that are not this test's subject.
CHANGES: dict[str, tuple[typing.Any, typing.Any]] = {
	"title": ("A title", "Another title"),
	"description": ("Some prose", "Different prose"),
	"body": ("Some prose", "Different prose"),
	"status": ("open", "blocked"),
	"type": ("task", "bug"),
	"importance": (1, 5),
	"urgency": (1, 5),
	"estimate": ("30m", "2h"),
	"tags": (["alpha"], ["beta"]),
	#: **Every one of these keeps `snoozed_until` on or before `due_at`**, which §10.7 enforces
	#: and which a pair chosen for tidiness walks straight into: the refusal names the *other*
	#: field, so a `due` moved back before the fixture's snooze fails as a snooze problem.
	"due": ("2026-12-01", "2027-01-01"),
	"due_is_all_day": (True, False),
	"starts": ("2026-11-01", "2026-11-15"),
	"starts_is_all_day": (True, False),
	"snooze": ("2026-10-01", "2026-11-01"),
	"snoozed_is_all_day": (True, False),
	"recurrence": ("every day", "every week"),
	"recurrence_anchor": ("completion", "schedule"),
	"timezone": ("Europe/London", "Australia/Sydney"),
}

#: The same, for a document — where two of the names mean something else entirely.
CHANGES_FOR_DOCUMENT: dict[str, tuple[typing.Any, typing.Any]] = {
	"status": ("draft", "active"),
	"type": ("note", "decision"),
}

#: What has to arrive **beside** a field for it to mean anything, because several of these say
#: nothing on their own and the endpoints refuse them by name for saying it.
#:
#: *"Whether something is a whole day or a time says nothing on its own"* and *"a repeat needs a
#: date to repeat from"* are the instance's own words. Sent in the same body on a create and as
#: a change beforehand on an update — the same map, because it is the same requirement.
BESIDE: dict[str, dict[str, typing.Any]] = {
	"due_is_all_day": {"due": "2026-12-01"},
	"starts_is_all_day": {"starts": "2026-11-01"},
	"snoozed_is_all_day": {"snooze": "2026-10-01"},
	"recurrence": {"due": "2026-12-01"},
	"recurrence_anchor": {"recurrence": "every month", "due": "2026-12-01"},
}

#: Where a request field is read back under a name `WRITTEN_AS` does not carry, with the reason.
#:
#: `text` is §6.13's captured line, which writes several fields at once — a title, dates, a
#: project, tags — so it has no single counterpart and `WRITTEN_AS` would be claiming one. The
#: title is the part that always moves, which is enough to say the line was read at all;
#: `tests/test_capture.py` is what asks what it made of the rest.
ALSO_READ_AS: dict[str, set[str]] = {"text": {"title"}}

#: Request fields that legitimately leave nothing to read back, each with the argument.
#:
#: **Every entry is a field whose *effect* is somewhere other than the entity**, not a field
#: nobody got round to wiring — which is the whole distinction this file exists to draw, and the
#: reason an entry needs a sentence rather than a name.
NOT_STORED: dict[str, str] = {
	"expected_version": "a precondition rather than a value (§8.9): it is compared and "
	"discarded, and `tests/test_api_concurrency.py` is what proves it was read",
	"recurrence_trigger": "only 'completion' is built, so there is no second value to set — "
	"`#94` refuses 'time' by name until a date-ranged view can expand it",

	#: **The one entry here that looks exactly like the defect**, and it took driving to tell
	#: them apart. `timezone` on a change is the zone *this request's dates are read in*, not a
	#: field being assigned: `#1014` writes it back only when a date actually moves, because
	#: editing a title from another country must not silently re-render every date on the task.
	#: So `PATCH {"timezone": …}` alone answers 200 and changes nothing, correctly.
	#:
	#: **Worth knowing about the guard next door**: `test_api_writability` is satisfied for the
	#: view's `timezone` by this field having the same name, and the two mean different things.
	#: A name match is all that guard can ask, which is the gap this file was written to cover.
	"timezone": "the zone this request's dates are resolved in, not a value being assigned — "
	"`#1014` writes it back only when a date moves",
}


class Ground(typing.NamedTuple):
	"""One installation, one item of each kind, and everything they can be pointed at."""

	world: test_api_tasks.World
	local: subroutine.clients.local.Client
	remote: subroutine.clients.http.Client
	task: int
	document: int
	project: str
	other_project: str
	other_workspace: str
	other_user: uuid.UUID
	spare_task: int
	spare_document: int


@pytest.fixture
def ground (session: sqlalchemy.orm.Session) -> Ground:
	"""An instance holding enough for any one field to be given two different values."""

	world = test_api_tasks._world(session)
	unique = uuid.uuid4().hex[:8]

	# **A create names its workspace in the *body*** — `POST /v1/tasks` and `POST /v1/documents`
	# take no query parameters at all and refuse one by name. A change names it in the query.
	# Two spellings for one thing, and the endpoints are the authority on which.
	here = {"workspace_id": world.workspace.slug}
	# **In the body here, and in the query everywhere else.** `POST /v1/projects` takes no query
	# parameters at all and refuses one by name, which is `#898` working.
	made = world.call("POST", "/v1/projects", json={
		"key": f"other-{unique}", "title": "Other",
		**here,
	})
	# **A second of each thing a field can point at.** A field whose two values are "this one"
	# and "nothing" cannot be told apart wherever null means *the default*: a document created
	# with no owner is owned by whoever wrote it, so `(the writer, null)` reads the same twice
	# and looks exactly like a dropped field.
	elsewhere = world.call(
		"POST", "/v1/workspaces", json={"slug": f"other-{unique}", "title": "Elsewhere"}
	)
	somebody = subroutine.domain.users.create(
		session, username=f"other-{unique}", actor=None
	)

	session.flush()

	# **And joined to the workspace**, because a document's owner is refused by name unless they
	# are a member — which is the check working, and is why a second account alone is not a
	# second owner.
	subroutine.domain.workspaces.add_member(
		session, workspace=world.workspace, user=somebody, role_key="member", actor=None
	)
	session.flush()

	for answer in (made, elsewhere):
		assert answer.status_code == 201, answer.text

	# **Made with a value in every field that another field qualifies**, because several of them
	# say nothing on their own: `due_is_all_day` describes a deadline that has to exist, and the
	# endpoint refuses the flag by name without it.
	#
	# **And deliberately without a repeat**, which cost an hour: a task created with one is a
	# template plus a minted occurrence, and `domain/recurrence` does not carry a snooze on to
	# an occurrence — with the argument written there. So a fixture that set a repeat here read
	# `snoozed_until` back as null and looked exactly like the defect this file is for.
	task = world.call("POST", "/v1/tasks", json={
		**here,
		"title": "Under test",
		"due": "2026-12-01",
		"starts": "2026-11-01",
		"snooze": "2026-10-01",
		"estimate": "1h",
	})
	spare_task = world.call(
		"POST", "/v1/tasks", json={**here, "title": "Somewhere to go"}
	)
	document = world.call(
		"POST", "/v1/documents", json={**here, "title": "Under test", "body": "."}
	)
	spare = world.call(
		"POST", "/v1/documents", json={**here, "title": "Elsewhere", "body": "."}
	)

	for answer in (task, spare_task, document, spare):
		assert answer.status_code == 201, answer.text

	# **The same installation reached both ways**, so a field can be given two values through a
	# client and read back through the same one. `clients/local` and `clients/http` assemble
	# what they send quite differently — the second builds a body by hand — so a field dropped
	# in one is exactly the divergence `test_transport_equivalence` exists for.
	# **`local_user` is named**, because this instance holds two accounts by the time the
	# fixture is built and the local client otherwise refuses: *"this database has more than one
	# account, so there is no way to tell whose to-do list to show"*. §12.1a's guess is for one
	# person on a laptop, and a second one is what a document's owner needed.
	# **And `database_url` names the database this really uses**, though the factory is injected
	# and nothing connects through it. `clients/local` translates any `SQLAlchemyError` into
	# *"no Subroutine instance has been set up here yet"* when `has_no_instance_yet()` is true —
	# which it is for the default SQLite path in a test — so every real failure arrived wearing
	# a message about a database nothing was reading, and the actual error was thrown away.
	bound = session.get_bind()
	local = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(
			dev_mode=True,
			local_user=world.user.username,
			database_url=bound.engine.url.render_as_string(hide_password=False),
		),
		session_factory=api_support.factory_for(session),
	)
	remote = subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://tasks.example.com"),
		token=world.secret,
		transport=api_support.SyncTransport(world.application),
		base_url=api_support.BASE_URL,
	)

	return Ground(
		world=world,
		local=local,
		remote=remote,
		task=task.json()["ref"],
		document=document.json()["ref"],
		project=task.json()["project_key"],
		other_project=made.json()["key"],
		other_workspace=elsewhere.json()["slug"],
		other_user=somebody.id,
		spare_task=spare_task.json()["ref"],
		spare_document=spare.json()["ref"],
	)


def _reads_back () -> dict[str, set[str]]:
	"""Return, per request field, the view fields it could be read back from.

	Inverted from ``test_api_writability.WRITTEN_AS`` rather than written again — that map is
	where this project already records the two spellings of one field, and a second copy would
	be the defect both files exist to catch. A set because two view fields may share one request
	field: ``parent`` writes a task's parent and a document's, and each view calls it its own
	thing.
	"""

	found: dict[str, set[str]] = {}

	for viewed, requested in test_api_writability.WRITTEN_AS.items():
		found.setdefault(requested, set()).add(viewed)

	return found


def _cases () -> list[tuple[str, str, str]]:
	"""Every (kind, model, field) a changing endpoint declares.

	Read off the models, so this is the population rather than a list of what somebody thought
	of — which is the same derivation `#501` and `#661` already use, and the only arrangement
	under which a field added tomorrow is covered without anybody remembering.
	"""

	found: list[tuple[str, str, str]] = []

	for kind, _view, create, changing in test_api_writability.SURFACES:
		# **The create as well as the changes**, because a field can be dropped on the way in
		# exactly as it can on the way past — `#918` was accepted by *both* and discarded by
		# both. A create has no "before", so the pair is two items rather than two writes to
		# one, and what is compared is the two answers.
		for model in (create, *changing):
			for field in model.model_fields:
				found.append((kind, model.__qualname__.split(".")[0], field))

	return found


CASES = [one for one in _cases() if one[2] not in NOT_STORED]

#: Values that can only be named once the instance exists — a project to move to, a person to
#: assign to, an item to sit under. Kept apart from :data:`CHANGES` rather than folded in, so
#: that one is a table anybody can read and this is the handful that genuinely cannot be.
def _from_the_instance (
	kind: str, field: str, ground: "Ground"
) -> tuple[typing.Any, typing.Any] | None:
	"""Return the pair for a field whose values are things this instance holds."""

	# **A parent is of the item's own kind**, and a ref that names the other one is refused by
	# name — `#488`, which is the refusal working rather than anything to route around.
	under = ground.spare_task if kind == "task" else ground.spare_document

	return {
		"project": (ground.project, ground.other_project),
		"text": ("Buy milk", "Call the dentist"),
		"workspace_id": (ground.world.workspace.slug, ground.other_workspace),
		"parent_task_id": (str(ground.spare_task), None),
		"owner_id": (str(ground.world.user.id), str(ground.other_user)),
		"assignee": (ground.world.user.username, None),
		"parent": (str(under), None),
		"supersedes": (str(ground.spare_document), None),
	}.get(field)


def _values (kind: str, field: str, ground: Ground) -> tuple[typing.Any, typing.Any]:
	"""Return the two values to give this field, or fail saying what is missing."""

	pair = (
		_from_the_instance(kind, field, ground)
		or (CHANGES_FOR_DOCUMENT.get(field) if kind == "document" else None)
		or CHANGES.get(field)
	)

	assert pair is not None, (
		f"{kind}.{field} is accepted by a changing endpoint and this file does not know what "
		f"to give it. Add two values to CHANGES — or, if there is nothing to read back, record "
		f"it in NOT_STORED with the reason."
	)

	return pair


def _changed (
	kind: str, model: str, field: str, ground: Ground, value: typing.Any
) -> dict[str, typing.Any]:
	"""Send one field one value, and read the whole entity back."""

	if model == "Create":
		return _made(kind, field, ground, value)

	# **Every request names a workspace**, because this instance holds two — one being the whole
	# point of `workspace_id` having two values to be given. An instance with one workspace lets
	# every request leave it out, which is `#1040`'s lesson about *one of a thing* arriving in a
	# fixture rather than in the product.
	here = f"?workspace_id={ground.world.workspace.slug}"
	where = f"/v1/{kind}s/{ground.task if kind == 'task' else ground.document}"
	path = f"{where}/move{here}" if model == "Move" else f"{where}{here}"
	method = "POST" if model == "Move" else "PATCH"

	answer = ground.world.call(method, path, json={field: value})

	assert answer.status_code == 200, (
		f"{method} {path} with {{{field!r}: {value!r}}} answered {answer.status_code}: "
		f"{answer.text}. A refusal here is this file having chosen a value the endpoint "
		f"cannot take, not the endpoint being wrong."
	)

	read = ground.world.call("GET", f"{where}{here}")

	assert read.status_code == 200, read.text

	return typing.cast(dict[str, typing.Any], read.json())


def _made (
	kind: str, field: str, ground: Ground, value: typing.Any
) -> dict[str, typing.Any]:
	"""Make one item with this field set, and return what came back.

	**A title and a body every time**, because a create has required fields and this is asking
	about one optional one. The answer is the item as stored, so no second read is needed.
	"""

	# **No title where the captured line is the subject**, because §6.13's line *is* the title
	# and an explicit one beside it wins — so both readings would be the title this file wrote
	# and the line would be proved by nothing.
	body: dict[str, typing.Any] = {} if field == "text" else {"title": f"Made with {field}"}

	if kind == "document":
		body["body"] = "."

	# Named unless it is the thing being asked about, in which case the value is the answer.
	if field != "workspace_id":
		body["workspace_id"] = ground.world.workspace.slug

	body.update(BESIDE.get(field, {}))
	body[field] = value

	answer = ground.world.call("POST", f"/v1/{kind}s", json=body)

	assert answer.status_code == 201, (
		f"POST /v1/{kind}s with {{{field!r}: {value!r}}} answered {answer.status_code}: "
		f"{answer.text}. A refusal here is this file having chosen a value the endpoint "
		f"cannot take, not the endpoint being wrong."
	)

	return typing.cast(dict[str, typing.Any], answer.json())


@pytest.mark.parametrize(
	("kind", "model", "field"), CASES, ids=[f"{one[0]}.{one[1]}.{one[2]}" for one in CASES]
)
def test_every_field_a_change_accepts_reaches_what_stores_it (
	ground: Ground, kind: str, model: str, field: str
) -> None:
	"""Give one field two values in turn, and the entity read back must differ between them."""

	first, second = _values(kind, field, ground)

	if model != "Create":
		for name, value in BESIDE.get(field, {}).items():
			_changed(kind, "Update", name, ground, value)

	before = _changed(kind, model, field, ground, first)
	after = _changed(kind, model, field, ground, second)

	viewed = ALSO_READ_AS.get(field) or _reads_back().get(field, {field})
	moved = {name for name in viewed if before.get(name) != after.get(name)}

	assert moved, (
		f"{kind}.{field} was set to {first!r} and then to {second!r}, and "
		f"{sorted(viewed)} read the same both times "
		f"({ {name: before.get(name) for name in sorted(viewed)} }). "
		f"The endpoint accepted the field and nothing downstream did anything with it — "
		f"which is what a widened signature over an unwidened body looks like from outside."
	)


def test_every_field_excused_here_is_still_one_a_change_accepts () -> None:
	"""The other direction, so this file cannot excuse something that has gone.

	A stale entry reads as a considered decision about a field that no longer exists, and it
	silently excuses whatever later takes the name.
	"""

	declared = {field for _kind, _model, field in _cases()}
	unknown = sorted(field for field in NOT_STORED if field not in declared)

	assert not unknown, (
		f"NOT_STORED names {unknown}, which no changing endpoint accepts any more."
	)


def test_the_cases_come_from_the_models_rather_than_from_a_list () -> None:
	"""The floor. A derivation that stopped reading anything reports no offenders at all.

	`#405`: this test reports what it *found*, so an empty walk reads exactly like a clean one —
	and both directions above are then vacuous rather than failing.
	"""

	assert len(_cases()) > 20, (
		f"only {len(_cases())} fields were found across every changing model, so the walk has "
		f"stopped reading them and every case below it is checking nothing."
	)



#: The client methods that write an item's own fields, and how to read the item back. A create
#: answers with the item; everything else is read again through the same client, which is what
#: makes the answer the *stored* thing rather than what the method was told.
#:
#: **This is the surface `#854`'s defect actually lived on**: `clients/base.Client.update` was
#: widened to take ``starts`` and ``snooze`` and both clients dropped them before the wire — the
#: signature grew and the body dict did not. `test_reach` compares method names and then field
#: names on a signature; neither asks what the body carries.
CLIENT_WRITES: tuple[tuple[str, str, bool], ...] = (
	("task", "capture", True),
	("task", "update", False),
	("task", "move", False),
	("task", "schedule", False),
	("document", "create_document", True),
	("document", "update_document", False),
)

#: Arguments that say *which* item, never what it holds. Excluded rather than excused, because
#: they are not fields of anything and a register of them would read as a list of gaps.
NOT_A_FIELD = frozenset({"ref", "workspace", "entity_type"})


def _client_cases () -> list[tuple[str, str, str]]:
	"""Every (kind, method, field) a writing client method declares.

	Off the signature rather than off a list, so a client widened tomorrow is covered tomorrow —
	which is precisely how the defect this file is named for got in: the signature was the thing
	that changed, and everything checking it read the signature.
	"""

	found: list[tuple[str, str, str]] = []

	for kind, method, _creates in CLIENT_WRITES:
		declared = inspect.signature(getattr(subroutine.clients.base.Client, method)).parameters

		for field in declared:
			if field in NOT_A_FIELD or field == "self":
				continue

			found.append((kind, method, field))

	return found


CLIENT_CASES = [one for one in _client_cases() if one[2] not in NOT_STORED]

#: What a create needs beside the field being asked about, per method.
REQUIRED: dict[str, dict[str, typing.Any]] = {
	# **With a deadline in the line**, because a repeat needs a date to repeat from and §6.13's
	# grammar is where a captured task gets one.
	"capture": {"text": "Under test by 2026-12-01"},
	"create_document": {"title": "Under test", "body": "."},
}


def _as_declared (annotation: typing.Any, value: typing.Any) -> typing.Any:
	"""Return ``value`` in the shape this argument is declared to take.

	The same value means the same thing on every surface and is *spelled* differently on some:
	a ref is a string over HTTP because a path segment is, and an ``int`` to a client method
	that says so; :meth:`schedule` predates §9.3's grammar reaching a client and takes a
	``datetime.date`` where everything else takes the expression.

	**Read off the annotation rather than listed per method**, so a second such argument needs
	nothing written here.

	**And handing a client the wrong shape is not a way to find a defect**, which is worth
	saying because it looked like one: ``move(parent="2")`` against a declared ``int | None``
	reaches PostgreSQL as ``task.ref = $5::VARCHAR`` and fails with *"operator does not exist:
	integer = character varying"*, where SQLite coerces and answers. That is a caller ignoring a
	type mypy would refuse, so it belongs here rather than on the product.
	"""

	if not isinstance(value, str):
		return value

	if "date" in str(annotation):
		return datetime.date.fromisoformat(value)

	if "int" in str(annotation) and value.isdigit():
		return int(value)

	return value


def _through (
	client: typing.Any, kind: str, method: str, field: str, ground: Ground, value: typing.Any
) -> dict[str, typing.Any]:
	"""Send one field one value through one client, and read the item back through it."""

	creates = next(one[2] for one in CLIENT_WRITES if one[1] == method)
	declared = inspect.signature(getattr(subroutine.clients.base.Client, method)).parameters
	body: dict[str, typing.Any] = dict(REQUIRED.get(method, {}))

	# **Only the companions this method actually has.** `capture` takes no `due`: §6.13's line
	# carries the date itself, which is why its text below has one in it. A companion sent to a
	# method that does not declare it is a `TypeError` about the test rather than the product.
	body.update({
		name: value for name, value in BESIDE.get(field, {}).items() if name in declared
	})
	body[field] = _as_declared(declared[field].annotation, value)

	if not creates:
		body["ref"] = ground.task if kind == "task" else ground.document

	# **Named on every call**, because this instance holds two workspaces — see `_changed`.
	body.setdefault("workspace", ground.world.workspace.slug)

	answered = getattr(client, method)(**body)
	made = getattr(answered, "task", answered)
	ref = made.ref if creates else body["ref"]

	read = (client.task if kind == "task" else client.document)(
		ref=ref, workspace=ground.world.workspace.slug
	)

	return typing.cast(dict[str, typing.Any], read.model_dump(mode="json"))


@pytest.mark.parametrize("transport", ("local", "remote"))
@pytest.mark.parametrize(
	("kind", "method", "field"),
	CLIENT_CASES,
	ids=[f"{one[0]}.{one[1]}.{one[2]}" for one in CLIENT_CASES],
)
def test_every_field_a_client_accepts_reaches_the_wire (
	ground: Ground, transport: str, kind: str, method: str, field: str
) -> None:
	"""Give one field two values through a client, and what that client reads back must move."""

	client = ground.local if transport == "local" else ground.remote
	first, second = _values(kind, field, ground)

	before = _through(client, kind, method, field, ground, first)
	after = _through(client, kind, method, field, ground, second)

	viewed = ALSO_READ_AS.get(field) or _reads_back().get(field, {field})
	moved = {name for name in viewed if before.get(name) != after.get(name)}

	assert moved, (
		f"{transport}.{method}({field}=…) was given {first!r} and then {second!r}, and "
		f"{sorted(viewed)} read the same both times "
		f"({ {name: before.get(name) for name in sorted(viewed)} }). "
		f"The client accepted the argument and nothing it sent carried it — which is exactly "
		f"what `#854` shipped, and what a guard reading the signature cannot see."
	)


def test_the_client_cases_come_from_the_signatures () -> None:
	"""The floor, for `_client_cases`'s own walk. `#405`: an empty walk reads as a clean one."""

	assert len(_client_cases()) > 30, (
		f"only {len(_client_cases())} fields were found across every writing client method, so "
		f"the walk has stopped reading them."
	)
