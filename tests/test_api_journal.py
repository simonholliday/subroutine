"""Reading what happened rather than what changed — item `#1430`, decision `#1429`.

`tests/test_api_changes.py` drives the audit log: raw, cheap, resumable, and what a client
polling should read. This drives the **join**, which is the whole of the difference — and the
measurement that produced it is worth restating, because every case here is one of its numbers.

One real day on this instance, 450 events: **130 of them `comment.created` carrying no body at
all**, 51 field-changes whose values were bare UUIDs, and an actor column that was a UUID on
every single row. So *"run me through what we did on Friday"* was answerable only by reading
every comment individually — which is not a feature, it is a list of things to go and look up.
"""

import datetime
import json
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.cli.personal
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.domain.events
import subroutine.domain.journal
import subroutine.domain.text
import subroutine.domain.users
import subroutine.mcp.tools
import subroutine.views
import test_api_tasks

#: Long enough ago that nothing here depends on the hour the suite runs at.
LONG_AGO = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)

#: What somebody wrote, with enough in it that a truncation would be obvious.
SAID = "Reproduced on 3.11 only. The fix in the other one does not apply here."


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation with a history to read."""

	return test_api_tasks._world(session)


def _settled (session: sqlalchemy.orm.Session, *, seconds: int = 2) -> None:
	"""Move every event past the feed's watermark, which the journal inherits.

	The same helper `tests/test_api_changes.py` carries and for its reason: the rule under test
	is not *how* a row got to be a second old.
	"""

	shift = datetime.timedelta(seconds=seconds)

	for event in session.scalars(sqlalchemy.select(subroutine.db.models.activity.Event)):
		event.created_at = event.created_at - shift

	session.flush()


def _entries (world: test_api_tasks.World, **query: typing.Any) -> list[dict[str, typing.Any]]:
	"""Read the journal and return its entries, failing loudly on anything but a 200."""

	answered = world.call("GET", "/v1/journal", params=query)

	assert answered.status_code == 200, answered.text

	entries: list[dict[str, typing.Any]] = answered.json()["items"]

	return entries


def _a_task_with_a_comment (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> dict[str, typing.Any]:
	"""File a task, write on it, finish it — one small day of work to read back."""

	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"})

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	wrote = world.call("POST", f"/v1/tasks/{ref}/comments", json={"body": SAID})

	assert wrote.status_code == 201, wrote.text

	finished = world.call("POST", f"/v1/tasks/{ref}/complete")

	assert finished.status_code == 200, finished.text

	session.flush()
	_settled(session)

	return {"ref": ref, "comment": wrote.json()["id"]}


def test_the_journal_says_what_a_comment_actually_said (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""**The one thing the feed omits, and 29% of a real day's events are it** — `#1430`.

	A `comment.created` event names the comment as its entity and carries nothing about its
	contents, so the audit log reports only that somebody wrote something. That is correct for
	an audit and useless for a report, and it is the reason this route exists at all.
	"""

	_a_task_with_a_comment(world, session)

	said = [entry["said"] for entry in _entries(world, limit=200) if entry["said"]]

	assert SAID in said, f"the journal did not carry what was written: {said}"

	# **And the audit log still does not**, which is the other half of decision `#1429`: two
	# reads of one store, and only one of them joins. A journal that worked by changing what is
	# *written* would show up here.
	feed = world.call("GET", "/v1/changes", params={"limit": 200})

	assert feed.status_code == 200, feed.text
	assert SAID not in feed.text, (
		"the comment's body reached the audit log, so this was built by writing more rather "
		"than by joining — which is `#578`'s bug made worse on 29% of the feed"
	)


def test_a_journal_entry_names_who_did_it (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`actor_user_id` is a UUID on every event, so a journal of them says a UUID did everything.

	Rendered through `principal_named`, which is the same function a row, a listing, `show` and
	the browser's roster use — `#1266`'s family, so an agent reads here exactly as it reads
	beside a task.
	"""

	_a_task_with_a_comment(world, session)

	actors = {entry["actor"] for entry in _entries(world, limit=200)}

	assert actors, "no entries at all, so this asserts nothing"
	assert f"@{world.user.username}" in actors, (
		f"the journal did not name who acted: {actors}"
	)

	# **Null is a real answer and not a failure to look**, which is why this is a subset rather
	# than an equality — and the first version of this test asserted equality and was wrong
	# about the design. `event.actor_user_id` is nullable precisely so an action the instance
	# took itself can say it had no person behind it, and bootstrapping a workspace is one:
	# `domain.bootstrap` runs before any principal exists, which is the single legitimate
	# `actor=None` in this codebase. A journal that invented a name for those would be
	# attributing work to somebody who did not do it.
	assert actors <= {f"@{world.user.username}", None}, (
		f"somebody other than the one account in this fixture is named: {actors}"
	)


def test_an_entry_says_what_its_item_is_where_it_is_filed_and_which_way_it_came_in (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#2727`. *A bug filed in web* is the sentence, and an entry carried neither half of it.

	**And the door rather than the credential** (Simon, 2026-09-16). Every call here presents a
	token with a title, and the title is nowhere in what the journal says, where the door is on
	every entry the token wrote.
	"""

	folder = world.call("POST", "/v1/projects", json={"key": "web", "title": "The website"})

	assert folder.status_code == 201, folder.text

	made = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "The header overlaps the menu", "type": "bug", "project": "web"},
	)

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	wrote = world.call("POST", f"/v1/tasks/{ref}/comments", json={"body": SAID})

	assert wrote.status_code == 201, wrote.text

	decided = world.call(
		"POST",
		"/v1/documents",
		json={"title": "The menu stays on top", "type": "decision", "project": "web"},
	)

	assert decided.status_code == 201, decided.text

	session.flush()
	_settled(session)

	entries = _entries(world, limit=200)
	about = [entry for entry in entries if entry["item_ref"] == ref]

	# **The comment names the item it was written on**, so both entries answer for the bug.
	assert {entry["entity_type"] for entry in about} == {"task", "comment"}, about
	assert {entry["item_type"] for entry in about} == {"bug"}, about
	assert {entry["item_project_path"] for entry in about} == {"web"}, about
	assert {entry["actor_interface"] for entry in about} == {"api"}, about

	# **A document's type is the workspace's document vocabulary**, loaded by the same batch.
	(paper,) = [entry for entry in entries if entry["item_ref"] == decided.json()["ref"]]

	assert (paper["item_type"], paper["item_project_path"]) == ("decision", "web"), paper

	(filed,) = [
		entry
		for entry in entries
		if entry["entity_type"] == "project" and entry["item_title"] == "The website"
	]

	# **A project is where it is filed, and it has no type.**
	assert filed["item_project_path"] == "web", filed
	assert filed["item_type"] is None, filed
	assert filed["actor_interface"] == "api", filed

	titles = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.ApiToken.title)
	).all()

	assert titles, "no credential here has a title, so its absence below proves nothing"

	for title in titles:
		assert title not in json.dumps(entries), (
			f"a journal entry named the credential {title!r}, which is its owner's word for "
			f"their own setup rather than anything the instance observed"
		)


def _long (marker: str) -> str:
	"""Return a text well past a comment's opening, ending in ``marker`` so a cut leaves it out."""

	sentence = "The measurement came back and it was not what anybody expected."

	return " ".join([sentence] * 12 + [marker])


def test_every_prose_field_is_a_whole_text () -> None:
	"""`#2728`: the field whose replacement is a revision is prose, so the journal leaves it out.

	**The two constants answer different questions** and the journal's is the wider one, so a
	kind given a prose field of its own is left out of the journal on the day it is declared.
	"""

	prose = set(subroutine.domain.events.PROSE_FIELD.values())

	assert prose <= subroutine.domain.journal.WHOLE_TEXTS, (
		f"{sorted(prose - subroutine.domain.journal.WHOLE_TEXTS)} count as prose for a revision "
		f"and are not left out of the journal, so a page of it carries them whole"
	)


def test_no_journal_entry_carries_a_whole_text (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#2728`, Simon's decision of 2026-09-16: *never* include full texts.

	**Every kind of thing that holds one**, not only the two a revision counts: a task's
	description, a document's body, a project's description and an edited comment. Each change
	is still reported, by its phrase, and the audit log still carries every text whole.
	"""

	texts = {
		"project": "PROJECT-TEXT-AFTER",
		"task before": "TASK-TEXT-BEFORE",
		"task after": "TASK-TEXT-AFTER",
		"document before": "DOCUMENT-TEXT-BEFORE",
		"document after": "DOCUMENT-TEXT-AFTER",
		"comment before": "COMMENT-TEXT-BEFORE",
		"comment after": "COMMENT-TEXT-AFTER",
	}

	answers = [
		world.call("POST", "/v1/projects", json={"key": "web", "title": "The website"}),
		world.call("PATCH", "/v1/projects/web", json={"description": texts["project"]}),
	]
	task = world.call(
		"POST", "/v1/tasks", json={"title": "Fix the menu", "description": texts["task before"]}
	)
	document = world.call(
		"POST", "/v1/documents", json={"title": "The menu", "body": texts["document before"]}
	)
	answers += [task, document]
	answers.append(
		world.call(
			"PATCH", f"/v1/tasks/{task.json()['ref']}", json={"description": texts["task after"]}
		)
	)
	answers.append(
		world.call(
			"PATCH",
			f"/v1/documents/{document.json()['ref']}",
			json={"body": texts["document after"]},
		)
	)
	wrote = world.call(
		"POST",
		f"/v1/tasks/{task.json()['ref']}/comments",
		json={"body": _long(texts["comment before"])},
	)
	answers.append(wrote)
	answers.append(
		world.call(
			"PATCH",
			f"/v1/comments/{wrote.json()['id']}",
			json={"body": _long(texts["comment after"])},
		)
	)

	for answer in answers:
		assert answer.status_code in (200, 201), answer.text

	session.flush()
	_settled(session)

	entries = _entries(world, limit=200)
	told = json.dumps(entries)

	for name, text in texts.items():
		assert text not in told, f"the journal carried the {name} text whole"

	# **Still reported, by its phrase**, so this is the text left out and not the change.
	prose = {
		(entry["entity_type"], change["field"])
		for entry in entries
		for change in entry["changed"]
		if change["field"] in subroutine.domain.journal.WHOLE_TEXTS
		and entry["action"] == "updated"
	}

	assert prose == {
		("project", "description"),
		("task", "description"),
		("document", "body"),
		("comment", "body"),
	}, prose

	for entry in entries:
		for change in entry["changed"]:
			if change["field"] in subroutine.domain.journal.WHOLE_TEXTS:
				assert change["before"] is None and change["after"] is None, entry

	# **And nothing became unreachable**: the audit log is where a whole text is read.
	feed = world.call("GET", "/v1/changes", params={"limit": 200})

	assert feed.status_code == 200, feed.text
	assert texts["task before"] in feed.text and texts["document after"] in feed.text


@pytest.mark.parametrize(
	("text", "expected"),
	[
		# Within the limit, and exactly at it: whole, and not marked.
		("short", ("short", False)),
		("a" * 10, ("a" * 10, False)),
		# A space exactly one past the limit ends a word at the limit.
		("abcd efghij klm", ("abcd", True)),
		("abcdefghij klm", ("abcdefghij", True)),
		# A line break is a place a word ends, and is kept inside the opening.
		("ab\ncd efghijklm", ("ab\ncd", True)),
		# One word longer than the limit is still cut, and so is one after leading space.
		("abcdefghijklmnop", ("abcdefghij", True)),
		("   abcdefghijklmnop", ("   abcdefg", True)),
	],
)
def test_an_opening_ends_at_a_word_and_says_whether_it_cut (
	text: str, expected: tuple[str, bool]
) -> None:
	"""`#2728`'s rule at a limit of ten, where every edge of it can be written out by hand."""

	assert subroutine.domain.text.opening(text, 10) == expected


def test_a_long_comment_is_cut_at_a_word_and_says_so (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#2728`: a comment is its opening, ended at a word, with a field saying there is more.

	**Three comments, one per answer**: one past the limit in words, one within it, and one
	unbroken word past it, which has no word to end at and is cut anyway.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"})

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	limit = subroutine.domain.journal.OPENING
	bodies = {
		"words": _long("the end"),
		"within": SAID,
		"one word": "x" * (limit + 120),
	}

	for body in bodies.values():
		wrote = world.call("POST", f"/v1/tasks/{ref}/comments", json={"body": body})

		assert wrote.status_code == 201, wrote.text

	session.flush()
	_settled(session)

	said = {
		entry["said"][:20]: entry
		for entry in _entries(world, limit=200)
		if entry["entity_type"] == "comment"
	}
	words, within, word = (said[body[:20]] for body in bodies.values())

	assert len(bodies["words"]) > limit
	assert words["said_truncated"] is True, words
	assert len(words["said"]) <= limit, words
	assert bodies["words"].startswith(words["said"]), words
	assert bodies["words"][len(words["said"])].isspace(), (
		f"the opening ends inside a word: {words['said'][-20:]!r}"
	)

	assert (within["said"], within["said_truncated"]) == (SAID, False), within

	assert (word["said"], word["said_truncated"]) == (bodies["one word"][:limit], True), word


def test_a_terminal_and_an_agent_are_told_where_a_comment_was_cut () -> None:
	"""`#2728`: both text surfaces draw the cut from the flag, and nothing from a whole comment.

	**Driven through each renderer**, so an opening that reads as a whole comment on either is a
	failure here rather than something an agent quotes as all that was said.
	"""

	def entry (cut: bool) -> subroutine.views.JournalEntry:
		"""Return a comment's entry, cut or not."""

		return subroutine.views.JournalEntry(
			seq=1,
			id=uuid.uuid4(),
			item_ref=42,
			item_title="Fix the deploy script",
			action="created",
			entity_type="comment",
			said="Reproduced on 3.11 only.",
			said_truncated=cut,
			created_at=LONG_AGO,
		)

	for render in (
		subroutine.cli.personal._journal_detail,
		subroutine.mcp.tools._journal_detail,
	):
		assert render(entry(False)) == ["Reproduced on 3.11 only."], render
		assert render(entry(True))[0].startswith("Reproduced on 3.11 only.…"), render


def test_the_journal_says_how_much_of_a_comment_it_carries (
	world: test_api_tasks.World,
) -> None:
	"""The published description names the length, so it is held against the constant."""

	published = world.call("GET", "/v1/openapi.json")

	assert published.status_code == 200, published.text

	described = published.json()["paths"]["/v1/journal"]["get"]["description"]

	assert f"at most {subroutine.domain.journal.OPENING} characters" in described, described


def test_an_items_journal_is_what_the_journal_says_about_that_item (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#2729`: the same entries `/v1/journal` gives about one item, read from its history.

	**Compared with the workspace's journal rather than described again**, so the two readings
	cannot come to say different things about the same event. A second task's entries are the
	ones that must not appear, and a page of two walked to the end is the whole of it.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"})
	other = world.call("POST", "/v1/tasks", json={"title": "Write the release notes"})

	assert made.status_code == 201 and other.status_code == 201, (made.text, other.text)

	ref = made.json()["ref"]
	answers = [
		world.call("PATCH", f"/v1/tasks/{ref}", json={"status": "in_progress"}),
		world.call("PATCH", f"/v1/tasks/{ref}", json={"description": "TASK-TEXT-WHOLE"}),
		world.call("POST", f"/v1/tasks/{ref}/comments", json={"body": _long("the end")}),
		world.call(
			"POST",
			f"/v1/tasks/{ref}/links",
			json={"target_type": "task", "target": other.json()["ref"], "link_type": "blocks"},
		),
		world.call("POST", f"/v1/tasks/{other.json()['ref']}/comments", json={"body": SAID}),
	]

	for answer in answers:
		assert answer.status_code in (200, 201), answer.text

	session.flush()
	_settled(session)

	whole = world.call("GET", f"/v1/tasks/{ref}/journal", params={"limit": 200})

	assert whole.status_code == 200, whole.text

	entries = whole.json()["items"]

	# **Newest first, as its history is.** `/v1/journal` returns its newest page in the order
	# things happened, so the two are compared entry by entry in one order rather than as lists.
	assert [entry["seq"] for entry in entries] == sorted(
		(entry["seq"] for entry in entries), reverse=True
	), "an item's journal is not newest first"

	expected = sorted(
		(entry for entry in _entries(world, limit=200) if entry["item_ref"] == ref),
		key=lambda entry: -entry["seq"],
	)

	assert {entry["entity_type"] for entry in entries} == {"task", "comment", "link"}, entries
	assert entries == expected, "one item's journal says something different from the journal"
	assert "TASK-TEXT-WHOLE" not in whole.text and "TASK-TEXT-WHOLE" in world.call(
		"GET", f"/v1/tasks/{ref}/events"
	).text, "the item's journal carried a whole text, or its history lost one"

	# **A page at a time, to the end**, which is the one thing this has that `/v1/journal` has not.
	walked: list[dict[str, typing.Any]] = []
	cursor = None

	while True:
		page = world.call(
			"GET",
			f"/v1/tasks/{ref}/journal",
			params={"limit": 2, **({} if cursor is None else {"cursor": cursor})},
		)

		assert page.status_code == 200, page.text

		walked += page.json()["items"]
		cursor = page.json()["page"]["next_cursor"]

		if cursor is None:
			break

	assert walked == entries, "walking an item's journal a page at a time lost or repeated entries"


def test_a_documents_journal_is_read_the_same_way (world: test_api_tasks.World) -> None:
	"""`#2729`: the document sibling, with a body changed and nothing of it carried."""

	made = world.call(
		"POST", "/v1/documents", json={"title": "The menu", "body": "DOCUMENT-TEXT-BEFORE"}
	)

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	changed = world.call("PATCH", f"/v1/documents/{ref}", json={"body": "DOCUMENT-TEXT-AFTER"})

	assert changed.status_code == 200, changed.text

	answered = world.call("GET", f"/v1/documents/{ref}/journal")

	assert answered.status_code == 200, answered.text

	entries = answered.json()["items"]

	assert all(entry["item_ref"] == ref for entry in entries), entries
	assert [entry["action"] for entry in entries][-1] == "created", entries

	(rewritten,) = [
		change
		for entry in entries
		for change in entry["changed"]
		if entry["action"] == "updated" and change["field"] == "body"
	]

	assert (rewritten["before"], rewritten["after"]) == (None, None), rewritten
	assert "DOCUMENT-TEXT" not in answered.text, "a document's journal carried its body"


def test_a_change_says_what_it_moved_between_and_not_which_rows (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""45 of 51 id-valued changes in one real day were `status_id` — `#1430`.

	`views.field_in_words` already maps the *column* to a phrase, so the feed can say *si changed
	how it is going*. **It does not resolve the values**, so what a reader is shown is
	`019fad98-431... -> 019fad98-431...`: two ids that are visibly different and mean nothing,
	on the single commonest change there is.
	"""

	_a_task_with_a_comment(world, session)

	moves = [
		change
		for entry in _entries(world, limit=200)
		for change in entry["changed"]
		if change["field"] == "status_id"
	]

	assert moves, "nothing changed status, so this asserts nothing"

	for move in moves:
		assert move["said"] == "how it is going", move
		assert move["before"] and move["after"], (
			f"a status change named neither side, so a reader cannot tell what happened: {move}"
		)
		assert move["before"] != move["after"], move


def test_no_journal_entry_ever_renders_an_identifier (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""**The guard for the whole design** — `#1430`, decision `#1429`.

	The rule taken is that a value we cannot name renders as **nothing** rather than as sixteen
	bytes of hex: the phrase alone — *changed how it repeats* — is shorter than a UUID and just
	as honest, where an id is noise a reader has to learn to skip.

	**So this catches a column nobody has declared a lookup for, on the day it is added**, which
	no test naming the columns we happen to resolve today could do. It is deliberately a
	property of the rendered answer rather than of `NAMED_BY`: a register can be complete and
	the renderer still leak, and the reader is who this is for.
	"""

	_a_task_with_a_comment(world, session)

	entries = _entries(world, limit=200)

	assert entries, "no entries at all, so this asserts nothing"

	shown = [
		value
		for entry in entries
		for value in [entry["actor"]]
		+ [side for change in entry["changed"] for side in (change["before"], change["after"])]
		if isinstance(value, str)
	]

	assert shown, "no values were rendered at all, so this could not fail"

	for value in shown:
		try:
			uuid.UUID(value)

		except ValueError:
			continue

		raise AssertionError(
			f"a journal entry rendered a bare identifier: {value!r}. A column with no lookup "
			f"in `domain.journal.NAMED_BY` must render its phrase and no value."
		)


def test_a_deleted_comment_is_absent_rather_than_quoted (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""Deletion is soft, so the body is still in the table — `#1430`.

	Without this the journal would be the one surface where a retracted paragraph is still
	readable, which is the same answer the mention index gives: a deleted comment stops
	mentioning anything, because a backlink to a sentence nobody can read is worse than none.
	"""

	made = _a_task_with_a_comment(world, session)
	removed = world.call("DELETE", f"/v1/comments/{made['comment']}")

	assert removed.status_code in (200, 204), removed.text

	session.flush()
	_settled(session)

	entries = _entries(world, limit=200)

	assert entries, "no entries at all, so this asserts nothing"
	assert not any(entry["said"] == SAID for entry in entries), (
		"a deleted comment is still quoted in the journal"
	)

	# **The entry itself stays**, which is the half that makes the assertion above mean
	# something rather than describing an empty answer: *somebody wrote and withdrew a comment*
	# is a real thing that happened, and an audit that dropped the row would be hiding it.
	assert any(entry["entity_type"] == "comment" for entry in entries), (
		"the whole entry vanished with the body, so the journal is now hiding that anything "
		"was written at all"
	)


def test_the_journal_takes_the_same_period_the_feed_does (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""One grammar, two routes — `#1431`, and the question Simon asked this for.

	*What did we do on Friday* is a period, and both readings of the store take it in the same
	spelling. Driven here as well as against the feed because the two compile it through
	different seams, and a route that declared no reader would answer 200 having ignored it.
	"""

	made = _a_task_with_a_comment(world, session)

	session.execute(
		sqlalchemy.update(subroutine.db.models.activity.Event).values(created_at=LONG_AGO)
	)
	session.flush()

	inside = _entries(world, limit=200, **{"created_at.gte": "2026-07-01"})
	outside = _entries(world, limit=200, **{"created_at.gte": "2026-08-15"})

	assert any(entry["item_ref"] == made["ref"] for entry in inside), (
		"a period containing the work found none of it"
	)
	assert not outside, f"a period after all of it found {len(outside)} entries"


def test_the_journal_says_what_it_covers (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#1085`'s rule, which the journal needs exactly as much as the feed.

	A credential narrowed away from a kind gets a journal of what it may read. Without this,
	*nothing happened on Friday* and *I am not shown that* are the same sentence — and the
	second is the one somebody would act on.
	"""

	_a_task_with_a_comment(world, session)

	answered = world.call("GET", "/v1/journal", params={"limit": 5})

	assert answered.status_code == 200, answered.text
	assert answered.json()["covers"], "the journal does not say which kinds it is a journal of"
