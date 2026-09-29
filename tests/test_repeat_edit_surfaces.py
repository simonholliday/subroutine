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
import subroutine.mcp.protocol
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
	# **A deferral is about the occurrence in front of you** (`SR#3705`, Simon on `SR#1308`).
	"snooze": "2026-09-02",
	"snoozed_is_all_day": True,
}


#: What a field with one answer needs beside it to be an edit at all (`SR#3705`). **Never a
#: field that asks**, so a companion cannot carry a request past the question for the field
#: being driven, which is what keeping each alone is for.
BESIDE: dict[str, dict[str, typing.Any]] = {
	# A deferral's clock describes a deferral, and the fixture has none to describe.
	"snoozed_is_all_day": {"snooze": "2026-09-02"},
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

	companion = BESIDE.get(field, {})

	assert not companion.keys() & ASKING.keys(), "a companion that asks would carry the request"

	instance.client.update(ref=instance.repeating, **{field: NOT_ASKED[field]}, **companion)


def test_a_deferral_answered_from_now_on_stays_with_the_occurrence (
	instance: Instance,
) -> None:
	"""`SR#3705`, Simon's decision on `SR#1308`: a deferral is about the occurrence in front of you.

	*Every one from now on* wrote the date to the series, and ``materialise`` clears it on each
	new occurrence - so the answer meant *just this one* while saying otherwise. It is still
	accepted, and it no longer reaches the series at all.
	"""

	changed = instance.client.update(
		ref=instance.repeating,
		snooze="2026-09-02",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
	)

	assert changed.snoozed_until is not None, "the occurrence in front of you was not deferred"
	assert changed.recurrence_template_ref is not None

	series = instance.client.task(ref=changed.recurrence_template_ref)

	assert series is not None
	assert series.snoozed_until is None, "the deferral reached the series"


def test_the_agents_tool_says_an_answer_about_a_deferral_changed_nothing (
	instance: Instance,
) -> None:
	"""`SR#3705`: sent with nothing else to decide, ``applies_to`` is reported, not refused.

	And left out, a deferral on a repeating item goes through, where it used to be refused for
	not saying which occurrences it was for.
	"""

	catalogue = {
		tool.name: tool
		for tool in subroutine.mcp.tools.catalogue(client=instance.client)
	}
	update = catalogue["subroutine_update"]

	plain = update.call({"ref": instance.repeating, "defer": "2026-09-02"})
	answered = update.call(
		{
			"ref": instance.repeating,
			"defer": "2026-09-03",
			"applies_to": subroutine.domain.tasks.FROM_NOW_ON,
		}
	)

	assert "applies_to changed nothing" not in plain, plain
	assert "applies_to changed nothing: a deferral is only ever for the occurrence" in answered, (
		answered
	)


def _the_repeat_itself (instance: Instance) -> int:
	"""Return the number of the series behind the fixture's repeating item, as ``show`` prints it."""

	shown = instance.client.task(ref=instance.repeating)

	assert shown is not None and shown.recurrence_template_ref is not None

	return shown.recurrence_template_ref


def test_a_deferral_given_the_repeat_itself_is_refused_naming_the_occurrence (
	instance: Instance,
) -> None:
	"""`SR#3748`, decision `SR#3795`: ``show`` prints *from repeat #1*, and that number is the series.

	A deferral written to the series reached nothing - the answer said *Deferred* and the
	occurrence in front of the person stayed as it was. It is refused now, by name, and the
	refusal says which number to defer instead: through the client every surface uses, over HTTP,
	where the status and the field are the contract, and through the agents' tool.
	"""

	series = _the_repeat_itself(instance)
	itself = f"#{series} is the repeat itself"
	instead = f"Defer #{instance.repeating}, the occurrence in front of you."

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		instance.client.update(ref=series, snooze="2026-09-02")

	assert itself in str(refused.value), str(refused.value)
	assert refused.value.hint == instead

	answered = api_support.call(
		instance.application,
		"PATCH",
		f"/v1/tasks/{series}",
		headers={"Authorization": f"Bearer {instance.token}"},
		json={"snooze": "2026-09-02"},
	)

	assert answered.status_code == 422, answered.text
	assert [error["field"] for error in answered.json()["errors"]] == ["snooze"]
	assert instead in answered.text, answered.text

	catalogue = {
		tool.name: tool
		for tool in subroutine.mcp.tools.catalogue(client=instance.client)
	}
	with pytest.raises(subroutine.errors.ValidationError) as told:
		catalogue["subroutine_update"].call({"ref": series, "defer": "2026-09-02"})

	read = subroutine.mcp.protocol._explained(told.value)

	assert itself in read and instead in read, read

	for ref in (series, instance.repeating):
		row = instance.client.task(ref=ref)

		assert row is not None and row.snoozed_until is None, f"#{ref} was deferred"


def test_the_repeat_itself_saved_with_its_own_empty_deferral_is_not_refused (
	instance: Instance,
) -> None:
	"""The browser's edit form sends every date on every save (`SR#3755`).

	So a series edited there sends its own empty deferral back unchanged, and refusing a deferral
	that was sent, rather than one that changes, would refuse every save of a series there.
	"""

	series = _the_repeat_itself(instance)
	kept = instance.client.update(ref=series, snooze=None)

	assert kept.snoozed_until is None


def test_a_deferral_on_the_repeat_itself_can_be_cleared_or_sent_back (
	instance: Instance,
) -> None:
	"""`SR#3898`, M-11 of the cold review of 2026-09-28: one written there could not be let go.

	A deferral given with a new repeat was stored on the series row, where nothing reads it, and the
	refusal above then turned down clearing it, and resending it to the minute, which is what the
	browser's edit form sends back. **Clearing cannot mislead, and a deferral sent back as it was
	is not a new one**, so both go through; setting one is refused as before.
	"""

	series = _the_repeat_itself(instance)
	shown = instance.client.task(ref=series)

	assert shown is not None

	row = instance.session.get(subroutine.db.models.work.Task, uuid.UUID(str(shown.id)))

	assert row is not None

	row.snoozed_until = datetime.datetime(2026, 10, 3, 9, 30, 15, 123456, tzinfo=datetime.UTC)
	row.snoozed_is_all_day = False
	instance.session.flush()

	resent = instance.client.update(ref=series, snooze="2026-10-03T09:30:00Z")

	assert resent.snoozed_until is not None, resent

	with pytest.raises(subroutine.errors.ValidationError):
		instance.client.update(ref=series, snooze="2026-10-04T09:30:00Z")

	cleared = instance.client.update(ref=series, snooze=None)

	assert cleared.snoozed_until is None, cleared

	# **The same instant with the other all-day-ness is a new deferral** (`SR#3941`): a timed one at
	# the start of a day and that day name one instant, and only one of them is the deferral the
	# row has - with nothing asking which, the whole suite passed.
	instance.session.refresh(row)
	row.snoozed_until = datetime.datetime(2026, 10, 5, tzinfo=datetime.UTC)
	row.snoozed_is_all_day = False
	instance.session.flush()

	with pytest.raises(subroutine.errors.ValidationError):
		instance.client.update(ref=series, snooze="2026-10-05")


def test_a_deferral_given_with_a_new_repeat_goes_on_its_first_occurrence (
	instance: Instance,
) -> None:
	"""`SR#3898`'s create half, decided by `#3915` (M-11 of the cold review of 2026-09-28).

	A deferral sent with a new repeat was written onto the series row, where nothing reads it, so the
	item showed at once. **It goes on the first occurrence, the one handed back**, and the series
	carries none.

	**Due three days out** (`SR#3987`): *every day* names its own day, so with no deadline the first
	one was due at the end of today, and from 21:00 UTC a deferral three hours on fell after it and
	was refused - a test that failed for three hours of every day.
	"""

	due = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3)).date().isoformat()
	made = api_support.call(
		instance.application,
		"POST",
		"/v1/tasks",
		json={
			"title": "Water the plants",
			"recurrence": "every day",
			"snooze": "now+3h",
			"due": due,
		},
		headers={"authorization": f"Bearer {instance.token}"},
	)

	assert made.status_code == 201, made.text

	first = made.json()

	assert first["snoozed_until"] is not None, "the occurrence handed back is not deferred"

	series = instance.client.task(ref=first["recurrence_template_ref"])

	assert series is not None and series.snoozed_until is None, "the series row was deferred"


def test_a_repeat_with_nothing_open_is_refused_saying_so (instance: Instance) -> None:
	"""The refusal names the occurrence only when there is one to name - `SR#3748`.

	A series whose rule is spent, or whose last occurrence was finished without a next one being
	made, has none, and a hint naming a number that is not there would send the reader to nothing.
	"""

	series = _the_repeat_itself(instance)
	shown = instance.client.task(ref=instance.repeating)

	assert shown is not None

	occurrence = instance.session.get(subroutine.db.models.work.Task, uuid.UUID(str(shown.id)))

	assert occurrence is not None

	occurrence.completed_at = NOW
	instance.session.flush()

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		instance.client.update(ref=series, snooze="2026-09-02")

	assert refused.value.hint == "It has no occurrence open to defer."


def test_skipping_the_repeat_itself_is_refused_naming_the_occurrence (
	instance: Instance,
) -> None:
	"""`SR#3748`: the other act that is only ever for one occurrence, which was refused already.

	It said *That is not one of a repeating series*, which is untrue of the repeat itself.
	"""

	series = _the_repeat_itself(instance)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		instance.client.skip(ref=series)

	assert f"#{series} is the repeat itself" in str(refused.value), str(refused.value)
	assert refused.value.hint == f"Skip #{instance.repeating}, the occurrence in front of you."


def test_claiming_the_repeat_itself_is_refused_naming_the_occurrence (
	instance: Instance,
) -> None:
	"""`SR#3942`, L-10 of the cold review of 2026-09-28: a claim held the series, not the work.

	It answered *Claimed*, and the occurrence stayed unclaimed and ready for anybody else, on the
	terminal and the agent tools alike. **Refused as a deferral and a skip are** (decision
	`SR#3795`), naming the occurrence to claim, and over HTTP too.
	"""

	series = _the_repeat_itself(instance)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		instance.client.claim(ref=series)

	assert f"#{series} is the repeat itself" in str(refused.value), str(refused.value)
	assert refused.value.hint == f"Claim #{instance.repeating}, the occurrence in front of you."

	over_http = api_support.call(
		instance.application,
		"POST",
		f"/v1/tasks/{series}/claim",
		headers={"authorization": f"Bearer {instance.token}"},
	)

	assert over_http.status_code == 422, over_http.text


def test_the_repeat_itself_is_refused_where_one_occurrence_is_meant (
	instance: Instance,
) -> None:
	"""`SR#3936`, L-5 of the cold review of 2026-09-28: the two transports disagreed.

	The local client turned the series away as it looked a ref up, for a move, a parent and a
	link, and the endpoint moved it, filed work under it and linked it. **Refused in the domain**,
	naming the occurrence as a deferral, a skip and a claim do, so both say it.
	"""

	series = _the_repeat_itself(instance)
	once = instance.once
	occurrence = f"#{instance.repeating}, the occurrence in front of you."
	attempts: list[tuple[typing.Callable[[], object], str, str, str, dict[str, typing.Any]]] = [
		(
			lambda: instance.client.move(ref=series, parent=once),
			f"Move {occurrence}",
			"ref",
			f"/v1/tasks/{series}/move",
			{"parent": str(once)},
		),
		(
			lambda: instance.client.move(ref=once, parent=series),
			f"Put it under {occurrence}",
			"parent",
			f"/v1/tasks/{once}/move",
			{"parent": str(series)},
		),
		(
			lambda: instance.client.capture(text="Bring the agenda", parent=series),
			f"Put it under {occurrence}",
			"parent_task_id",
			"/v1/tasks",
			{"title": "Bring the agenda", "parent_task_id": series},
		),
		(
			lambda: instance.client.link(ref=once, link_type="blocks", target=series),
			f"Link {occurrence}",
			"target",
			f"/v1/tasks/{once}/links",
			{"link_type": "blocks", "target": series},
		),
		(
			lambda: instance.client.link(ref=series, link_type="blocks", target=once),
			f"Link {occurrence}",
			"ref",
			f"/v1/tasks/{series}/links",
			{"link_type": "blocks", "target": once},
		),
	]

	for attempt, hint, field, path, body in attempts:
		with pytest.raises(subroutine.errors.ValidationError) as refused:
			attempt()

		assert f"#{series} is the repeat itself" in str(refused.value), str(refused.value)
		assert refused.value.hint == hint, refused.value.hint
		assert [error.field for error in refused.value.errors] == [field], refused.value.errors

		over_http = api_support.call(
			instance.application,
			"POST",
			path,
			headers={"authorization": f"Bearer {instance.token}"},
			json=body,
		)

		assert over_http.status_code == 422, (path, over_http.text)
		assert f"#{series} is the repeat itself" in over_http.json()["detail"], over_http.text


def test_deleting_the_repeat_itself_says_how_a_repeat_is_stopped (
	instance: Instance,
) -> None:
	"""`SR#3936`: the endpoint deleted the series, which the terminal turns down (`SR#1294`).

	In the trash, the series left its occurrence saying it repeats, with nothing that would bring
	the next. **Refused in the domain**, saying that a repeat is stopped by marking it done, and
	naming the occurrence for somebody who meant only that one, which may still be deleted.
	"""

	series = _the_repeat_itself(instance)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		instance.client.discard(ref=series)

	assert f"#{series} is the repeat itself" in str(refused.value), str(refused.value)
	assert refused.value.hint == (
		f"Mark #{series} done to stop it, keeping what it was and what it ran, or delete "
		f"#{instance.repeating} to drop only the occurrence in front of you."
	), refused.value.hint

	over_http = api_support.call(
		instance.application,
		"DELETE",
		f"/v1/tasks/{series}",
		headers={"authorization": f"Bearer {instance.token}"},
	)

	assert over_http.status_code == 422, over_http.text

	gone = instance.client.discard(ref=instance.repeating)

	assert gone.ref == instance.repeating, "the occurrence could not be deleted on its own"


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
