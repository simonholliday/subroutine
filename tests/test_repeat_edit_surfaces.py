"""Which occurrences an edit is for, on every surface that can write one — item ``#1252``.

Decision ``#1249``: a person is not aware that a repeating item is two rows, so **every edit
says whether it is for this one or for every one from now on**, and the answer is a conscious
choice rather than a rule per field that nobody could learn.

**The default flips here and that is a breaking change Simon took knowingly.** ``PATCH
/v1/tasks/42 {"starts": "3pm"}`` on a repeating item answered ``200`` before this and answers
``422`` now. The alternative was keeping the old behaviour — every edit landing on the
occurrence, nothing reaching the series — and he refused it: an agent silently getting *just
this one* is the whole defect ``#1247`` reports, where a correction lasted one turn of the
wheel and nothing said so.

**Three registers hold this rule and none of them is compared to another by name.** The domain
knows its own parameters, the client layer knows the arguments both the terminal and an agent
send, and neither list would be worth anything if the guard only checked that they agreed about
the words they share. So every argument is *driven*: given alone, on a real repeating item,
either it is refused for not saying or it is not. What each register claims is then checked
against what the code does rather than against the other register.
"""

import datetime
import inspect
import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.api.meta
import subroutine.clients.base
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.tasks
import subroutine.errors
import subroutine.mcp.tools
import subroutine.views

#: One instant, fixed. `#1245` is what a wall clock costs a fixture that dates from a constant.
NOW = datetime.datetime(2026, 8, 26, 9, 0, tzinfo=datetime.UTC)

#: The arguments to ``Client.update`` that are not fields at all — an address, the answer
#: itself, and the optimistic-concurrency check.
NOT_A_FIELD = frozenset({"self", "ref", "workspace", "applies_to", "expected_version"})

#: One usable value per argument that **asks**, so each can be driven on its own.
#:
#: **Alone, deliberately.** Sent together, one asking field would carry the whole request and a
#: register that had wrongly excused another would never be noticed.
ASKING: dict[str, typing.Any] = {
	"title": "Renamed",
	"description": "Why this exists",
	"type": "bug",
	"importance": 4,
	"urgency": 3,
	"estimate": "2h",
	"reminder": "1h",
	"assignee": None,
	"tags": ["evening"],
	"due": "2026-09-01",
	"due_is_all_day": True,
	"starts": "2026-09-04",
	"starts_is_all_day": True,
	"ends": "2026-09-05",
	"snooze": "2026-09-02",
	"snoozed_is_all_day": True,
	"project": "inbox",
}

#: One usable value per argument that decision `#1249` §1 says has only one answer.
NOT_ASKED: dict[str, typing.Any] = {
	"status": "in_progress",
	"recurrence": "every month",
	"recurrence_anchor": "completion",
	# ``completion`` rather than ``time``: a time-triggered repeat is refused by name as
	# unbuilt (`#94`), and a value the service turns down measures nothing about whether this
	# one is asked about.
	"recurrence_trigger": "completion",
	"timezone": "Etc/UTC",
}


class Instance(typing.NamedTuple):
	"""One instance holding a repeating item and an ordinary one."""

	client: subroutine.clients.local.Client
	application: typing.Any
	token: str
	session: sqlalchemy.orm.Session
	repeating: int
	once: int


@pytest.fixture
def instance (session: sqlalchemy.orm.Session) -> typing.Iterator[Instance]:
	"""Build an instance with one repeating item and one that does not repeat.

	**Both, because the rule has two directions.** An item that repeats is refused for not
	saying; one that does not is refused for saying. A fixture with only the first would pass
	against a build that asked about everything, which is the friction decision `#1249` §1 is
	written to avoid.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session,
		username=f"si-{uuid.uuid4().hex[:8]}",
		instance_name="Repeats",
		workspace_slug="home",
		timezone="Etc/UTC",
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Repeats"
	)
	actor = subroutine.domain.authentication.Principal(user=setup.user)

	repeating = subroutine.domain.tasks.create(
		session,
		project=setup.inbox,
		actor=actor,
		title="Stand-up",
		recurrence="every week",
		starts=NOW,
		now=NOW,
	)
	once = subroutine.domain.tasks.create(
		session, project=setup.inbox, actor=actor, title="Order more coffee", now=NOW
	)
	session.flush()

	factory = api_support.factory_for(session)
	settings = subroutine.config.Settings(dev_mode=True, default_timezone="Etc/UTC")
	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		settings,
		session_factory=factory,
		token=issued.value.get_secret_value(),
	)

	with client:
		yield Instance(
			client=client,
			application=api_support.build_app(factory),
			token=issued.value.get_secret_value(),
			session=session,
			repeating=repeating.ref,
			once=once.ref,
		)


def _arguments () -> frozenset[str]:
	"""Return every field ``Client.update`` can write, off the signature rather than a list.

	`#1268`'s lesson, one layer along: a hand-written population is a place for a field to be
	missing from, and every guard built on it inherits the gap in silence.
	"""

	return frozenset(
		inspect.signature(subroutine.clients.base.Client.update).parameters
	) - NOT_A_FIELD


def test_every_field_update_writes_is_in_one_register (
	instance: Instance,
) -> None:
	"""Nothing ``Client.update`` accepts is missing from both of the registers below.

	The two tests after this drive what these name. Without this one, a field added tomorrow
	would be driven by neither and both would go on passing — which is the exact shape of the
	defect `#1268` found in ``tasks._snapshot``.
	"""

	assert _arguments() == frozenset(ASKING) | frozenset(NOT_ASKED)
	assert not frozenset(ASKING) & frozenset(NOT_ASKED)
	assert frozenset(NOT_ASKED) == subroutine.clients.base.NEVER_ASKS


@pytest.mark.parametrize("field", sorted(ASKING), ids=sorted(ASKING))
def test_a_field_with_two_answers_is_refused_without_one (
	instance: Instance, field: str
) -> None:
	"""Each asking field, driven alone on a repeating item with nothing said.

	**The refusal names ``applies_to``**, which is the field an HTTP caller sends and the
	argument an agent's tool takes. It deliberately does not name ``title`` or ``due``: the
	names at that layer are this function's arguments rather than words anybody typed.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		instance.client.update(
			ref=instance.repeating, **{field: ASKING[field]}
		)

	assert [error.field for error in refused.value.errors] == ["applies_to"]
	assert refused.value.code == "missing_field"


@pytest.mark.parametrize("field", sorted(NOT_ASKED), ids=sorted(NOT_ASKED))
def test_a_field_with_one_answer_is_not_asked_about (
	instance: Instance, field: str
) -> None:
	"""Each exempt field, driven alone on a repeating item with nothing said.

	**This is the half that stops the refusal becoming a toll**, and it is the direction a
	register is least likely to be checked in: a guard that only proves fields *are* refused
	passes just as well against a build that refuses all of them.
	"""

	instance.client.update(ref=instance.repeating, **{field: NOT_ASKED[field]})


def test_an_answer_about_something_that_does_not_repeat_is_refused (
	instance: Instance,
) -> None:
	"""The mirror, and it is not politeness — an ignored argument is an inert control.

	This codebase has found three of those (`#247`, `#251`, `#303`): a value accepted,
	documented and read by nothing. Somebody who says *from now on* about a one-off has
	misunderstood something, and the cheapest moment to say so is the one where they said it.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		instance.client.update(
			ref=instance.once,
			title="Renamed",
			applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		)

	assert [error.field for error in refused.value.errors] == ["applies_to"]


def test_an_ordinary_item_is_never_asked (instance: Instance) -> None:
	"""Most of what anybody edits does not repeat, and nothing here may cost it anything."""

	changed = instance.client.update(ref=instance.once, title="Order the good coffee")

	assert changed.title == "Order the good coffee"


def test_the_two_predicates_answer_the_same_question (instance: Instance) -> None:
	"""``views.repeats`` and ``tasks.repeats`` are one rule written twice, on purpose.

	A client holds a rendered view and never a row, so the surface deciding whether to put the
	question to somebody cannot ask the domain's version. Driven against one real item at each
	end rather than compared as source, which would only prove the two read alike.
	"""

	for ref in (instance.repeating, instance.once):
		row = instance.session.scalars(
			sqlalchemy.select(subroutine.db.models.work.Task).where(
				subroutine.db.models.work.Task.ref == ref
			)
		).one()

		rendered = instance.client.task(ref=ref)

		assert rendered is not None
		assert subroutine.views.repeats(rendered) == subroutine.domain.tasks.repeats(row)


# --- The surfaces ---------------------------------------------------------------------------


def test_the_api_refuses_an_edit_that_does_not_say (instance: Instance) -> None:
	"""``PATCH`` answered 200 the day before this and answers 422 now — `#1252`.

	Driven over the real application rather than through the client, because the status code
	*is* the contract: a domain refusal that arrived as a 500, or as a 400, would be a
	different published promise with the same words in it.
	"""

	refused = api_support.call(
		instance.application,
		"PATCH",
		f"/v1/tasks/{instance.repeating}",
		headers={"Authorization": f"Bearer {instance.token}"},
		json={"title": "Renamed"},
	)

	assert refused.status_code == 422, refused.text
	assert refused.json()["code"] == "missing_field"
	assert [error["field"] for error in refused.json()["errors"]] == ["applies_to"]

	answered = api_support.call(
		instance.application,
		"PATCH",
		f"/v1/tasks/{instance.repeating}",
		headers={"Authorization": f"Bearer {instance.token}"},
		json={"title": "Renamed", "applies_to": "from_now_on"},
	)

	assert answered.status_code == 200, answered.text
	assert answered.json()["title"] == "Renamed"


def test_the_agents_tool_carries_the_answer (instance: Instance) -> None:
	"""An agent cannot edit a repeating item at all without this argument on the schema.

	§21.2's test at its sharpest: not *would an agent get this wrong* but *is it refused
	outright*. `#821` is the precedent — a tool publishing three of five link types — and the
	failure mode is the same, because an agent that is never told an argument exists has no
	reason to try it and so never learns there is a way to say this.
	"""

	catalogue = {
		tool.name: tool
		for tool in subroutine.mcp.tools.catalogue(client=instance.client)
	}
	schema = catalogue["subroutine_update"].schema

	assert "applies_to" in schema["properties"]

	changed = catalogue["subroutine_update"].call(
		{
			"ref": instance.repeating,
			"title": "Renamed by an agent",
			"applies_to": subroutine.domain.tasks.FROM_NOW_ON,
		}
	)

	assert "Renamed by an agent" in changed


def test_the_installation_publishes_both_answers (instance: Instance) -> None:
	"""``/v1/meta`` names the two words, read off the same constant the refusal lists.

	The one closed language here that a caller is *refused* for not speaking. Every other
	grammar published there is a convenience — write a date badly and the words stay in the
	title — so this is the one an agent most needs and the one it could least infer.
	"""

	published = api_support.call(
		instance.application,
		"GET",
		"/v1/meta",
		headers={"Authorization": f"Bearer {instance.token}"},
	).json()

	grammar = published["grammars"]["repeat_edits"]

	assert grammar["vocabulary"] == list(subroutine.domain.tasks.ANSWERS)
	assert "all" not in grammar["vocabulary"], (
		"'all' promises something about history that does not happen"
	)
