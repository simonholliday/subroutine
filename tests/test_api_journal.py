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
import types
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.cli.personal
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.work
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


def test_a_date_that_moved_without_its_flag_is_read_by_the_flag_the_item_has () -> None:
	"""`SR#2897`: `SR#1298`'s second half - the item's flag as it stands where it did not move.

	Every dated step in the journal's tests moved the all-day flag alongside the date, so the
	half that reads the item's own flag was never asked, and inverting it left them green. §6.5
	stores an all-day deadline at the last microsecond of its day, so getting it wrong writes a
	whole day as *23:59*.
	"""

	whole = subroutine.domain.events.Described(
		ref=1, title="Wanted", timezone="Europe/London", due_is_all_day=True
	)
	timed = whole._replace(due_is_all_day=False)
	moved = {"due_at": {"from": None, "to": "2030-09-18T22:59:59.999999+00:00"}}
	value = moved["due_at"]["to"]

	assert subroutine.views._dated_in_words("due_at", value, moved, "to", about=whole) == "2030-09-18"
	assert subroutine.views._dated_in_words(
		"due_at", value, moved, "to", about=timed
	) == "2030-09-18T23:59"


def test_a_repeat_recorded_before_it_was_recorded_whole_still_reads_as_one () -> None:
	"""`SR#2897`: an entry written before `SR#2825` names a repeat by its rule or its series.

	**Those are exactly what an upgraded instance holds** about its own history, and no test
	built one, so making either branch answer anything at all left the file green. Each is
	asked for the words the rule reads as, against a series stood in for by the one thing the
	branch reads of it.
	"""

	rule = "FREQ=WEEKLY;BYDAY=MO"
	series = uuid.uuid4()
	vocabulary = typing.cast(
		subroutine.views.Vocabulary,
		types.SimpleNamespace(
			parents={series: {"recurrence_rule": rule, "recurrence_anchor": None}}
		),
	)
	expected = subroutine.views._repeat_in_words(rule, None)

	def said (field: str, value: str) -> str | None:
		"""Return one older-shaped change's new side, as words."""

		return subroutine.views._moved_in_words(
			field, {field: {"from": None, "to": value}}, "to", vocabulary=vocabulary, about=None
		)

	assert "Monday" in expected, f"the rule did not read as words at all: {expected}"
	assert said("recurrence_rule", rule) == expected
	assert said("recurrence_template_id", str(series)) == expected


@pytest.mark.parametrize(
	("text", "expected"),
	[
		# Within the limit, and exactly at it: whole, and not marked.
		("short", ("short", False)),
		("a" * 10, ("a" * 10, False)),
		# A space exactly one past the limit ends a word at the limit.
		("abcd efghij klm", ("abcd", True)),
		("abcdefghij klm", ("abcdefghij", True)),
		# A line break is a space, with whatever whitespace surrounds it (`#2852`), and the limit
		# counts what is left.
		("ab\ncd efghijklm", ("ab cd", True)),
		("ab  \r\n\n  cd", ("ab cd", False)),
		# One word longer than the limit is still cut, and so is one after leading space.
		("abcdefghijklmnop", ("abcdefghij", True)),
		("   abcdefghijklmnop", ("   abcdefg", True)),
		# **A word that would cost most of the budget is cut too** (`SR#2892`): ending at the
		# space would keep two characters of ten.
		("ab cdefghijklmnop", ("ab cdefghi", True)),
		# And a boundary that keeps a third or more is still where it ends.
		("abcd efghijklmnop", ("abcd", True)),
	],
)
def test_an_opening_ends_at_a_word_and_says_whether_it_cut (
	text: str, expected: tuple[str, bool]
) -> None:
	"""`#2728`'s rule at a limit of ten, where every edge of it can be written out by hand."""

	assert subroutine.domain.text.opening(text, 10) == expected


def test_an_opening_keeps_its_budget_when_a_long_word_follows () -> None:
	"""`SR#2892`, measured by the cold review of 2026-09-18 at the journal's own limit.

	A link beginning just after the first sentence left *Fixed the thing. See* - 20 characters of
	140 - and threw the rest of the budget away.
	"""

	said = "Fixed the thing. See https://example.com/" + "a" * 160 + " for details"
	kept, cut = subroutine.domain.text.opening(said, subroutine.domain.journal.OPENING)

	assert cut, kept
	assert kept.startswith("Fixed the thing. See https://example.com/"), kept
	assert len(kept) == subroutine.domain.journal.OPENING, (len(kept), kept)


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


def test_a_comment_reads_on_one_line_in_the_journal (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#2852` (Simon, 2026-09-17): a heading, a blank line and a list took five lines of a journal.

	**A run of whitespace holding a line break is one space**, and the limit counts what is left,
	so an opening is one line of words however the comment was laid out.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"})

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	body = "**E6 is finished.**\n\nIt sorts the candidates:\r\n\n- **Clean:** pypdf  \n  and mammoth"
	wrote = world.call("POST", f"/v1/tasks/{ref}/comments", json={"body": body})

	assert wrote.status_code == 201, wrote.text

	session.flush()
	_settled(session)

	(entry,) = [
		entry
		for entry in _entries(world, limit=200)
		if entry["entity_type"] == "comment" and entry["item_ref"] == ref
	]

	assert (entry["said"], entry["said_truncated"]) == (
		"**E6 is finished.** It sorts the candidates: - **Clean:** pypdf and mammoth", False
	), entry


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

	# **Newest first, as its history is, and as the journal itself is** since `#2772` — so the
	# two are compared as lists, order and all.
	assert [entry["seq"] for entry in entries] == sorted(
		(entry["seq"] for entry in entries), reverse=True
	), "an item's journal is not newest first"

	expected = [entry for entry in _entries(world, limit=200) if entry["item_ref"] == ref]

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


def test_the_journal_answers_its_latest_newest_first_and_a_periods_first_forwards (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#2772`, Simon's decision of 2026-09-16: **the next page has to continue this one.**

	Read from the newest, the page after is the entries before it; read from the oldest, the
	entries after. The journal answered its latest page in the order things happened, which left
	a next page meaning nothing, and said *newest first* while doing it.
	"""

	for title in ("Call the dentist", "Pay the gas bill", "Book the car in", "Water the plants"):
		made = world.call("POST", "/v1/tasks", json={"title": title})

		assert made.status_code == 201, made.text

	session.flush()
	_settled(session)

	everything = [entry["seq"] for entry in _entries(world, limit=200)]
	latest = [entry["seq"] for entry in _entries(world, limit=2)]
	first = [entry["seq"] for entry in _entries(world, limit=2, oldest="true")]

	assert everything == sorted(everything, reverse=True), everything
	assert latest == sorted(everything, reverse=True)[:2], (latest, everything)
	assert first == sorted(everything)[:2], (first, everything)


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

	`views.field_in_words` already maps the *column* to a name, so the feed can say *si changed
	status*. **It does not resolve the values**, so what a reader is shown is
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
		assert move["said"] == "status" and move["quoted"], move
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


def _updated_lines (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session, ref: int
) -> list[subroutine.views.JournalEntry]:
	"""Return the updates a journal records for one item, oldest first, as the view reads them."""

	_settled(session)

	return [
		subroutine.views.JournalEntry.model_validate(entry)
		for entry in reversed(_entries(world, limit=200))
		if entry["item_ref"] == ref and entry["action"] == "updated"
	]


def test_a_change_is_written_as_a_person_reads_it (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""Decision `#2823` (Simon, 2026-09-17), read off real changes rather than a copy of the rules.

	**One line per fact, in plain names**: a date as a date in the item's own zone, with its
	o'clock only where one is stored; a duration with its unit; an empty side as *never* or
	*nobody*; a chosen name in quotes; and marking something done as its status alone. The same
	entries through the terminal and the agent tools, which differ only in how a date is written.
	"""

	created = world.call(
		"POST", "/v1/tasks", json={"title": "Water the plants", "timezone": "Europe/London"}
	)

	assert created.status_code == 201, created.text

	ref = created.json()["ref"]
	person = f"@{world.user.username}"

	for step in (
		# **The zone goes with every dated change**: a date sent without one is read in the
		# account's zone, and the item's zone moves to say so, which is a line of its own.
		{"due": "2030-09-18", "timezone": "Europe/London"},
		# 16:00 UTC is 17:00 in London in September, which is the item's zone and so the o'clock.
		{"due": "2030-09-18T16:00:00Z", "timezone": "Europe/London"},
		{"estimate": "30m"},
		{"reminder": "1h"},
		{"importance": 4, "urgency": 2},
		{"status": "blocked"},
		{"assignee": world.user.username},
		{"snooze": "2030-09-17", "timezone": "Europe/London"},
		{"title": "Go to the shop to water the plants"},
		{"status": "done"},
		{"type": "chore"},
	):
		answered = world.call("PATCH", f"/v1/tasks/{ref}", json=step)

		assert answered.status_code == 200, (step, answered.text)

	entries = _updated_lines(world, session, ref)

	assert [
		[(change.field, change.said, change.before, change.after) for change in entry.changed]
		for entry in entries
	] == [
		[("due_at", "deadline", None, "2030-09-18")],
		[("due_at", "deadline", "2030-09-18", "2030-09-18T17:00")],
		[("estimate_minutes", "time estimate", None, "30m")],
		[("reminder_minutes", "reminder", None, "1h before")],
		[("importance", "importance", None, "4"), ("urgency", "urgency", None, "2")],
		[("status_id", "status", "Open", "Blocked")],
		[("assignee_id", "assignee", None, person)],
		[("snoozed_until", "deferred until", None, "2030-09-17")],
		# **That it changed, and neither title** (`#2853`): the row shows the one it has now.
		[("title", "title", None, None)],
		[("status_id", "status", "Blocked", "Done")],
		# By its label, as the row shows it (`#2830`).
		[("type_id", "type", "Task", "Chore")],
	]

	terminal = [line for entry in entries for line in subroutine.cli.personal._journal_detail(entry)]
	agent = [line for entry in entries for line in subroutine.mcp.tools._journal_detail(entry)]

	assert terminal == [
		"deadline: never to Wed 18 Sep 2030",
		"deadline: Wed 18 Sep 2030 to Wed 18 Sep 2030 at 17:00",
		"time estimate: nothing to 30m",
		"reminder: nothing to 1h before",
		"importance: nothing to 4",
		"urgency: nothing to 2",
		'status: "Open" to "Blocked"',
		f"assignee: nobody to {person}",
		"deferred until: never to Tue 17 Sep 2030",
		"title",
		'status: "Blocked" to "Done"',
		'type: "Task" to "Chore"',
	], terminal
	assert agent == [
		"deadline: never to 2030-09-18",
		"deadline: 2030-09-18 to 2030-09-18T17:00",
		*terminal[2:8],
		"deferred until: never to 2030-09-17",
		*terminal[9:],
	], agent


def test_a_title_change_says_that_it_changed_and_carries_neither_title (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#2853` (Simon, 2026-09-17): *changed title from "…" to "…"* was a journal's longest line.

	The item's row shows the title it has now, so an entry says only that the title changed - a
	task's, a document's and a project's alike, since the rule is by field name - and the audit
	log still has both.
	"""

	titles = {
		"task": ("TASK-TITLE-BEFORE", "Water the plants"),
		"document": ("DOCUMENT-TITLE-BEFORE", "How the plants are watered"),
		"project": ("PROJECT-TITLE-BEFORE", "The garden"),
	}
	answers = [
		world.call("POST", "/v1/tasks", json={"title": titles["task"][0]}),
		world.call("POST", "/v1/documents", json={"title": titles["document"][0]}),
		world.call("POST", "/v1/projects", json={"key": "garden", "title": titles["project"][0]}),
	]

	for answer in answers:
		assert answer.status_code == 201, answer.text

	task, document, _project = (answer.json() for answer in answers)

	for path, title in (
		(f"/v1/tasks/{task['ref']}", titles["task"][1]),
		(f"/v1/documents/{document['ref']}", titles["document"][1]),
		("/v1/projects/garden", titles["project"][1]),
	):
		renamed = world.call("PATCH", path, json={"title": title})

		assert renamed.status_code == 200, renamed.text

	session.flush()
	_settled(session)

	entries = _entries(world, limit=200)
	told = json.dumps(entries)

	for kind, (before, _after) in titles.items():
		assert before not in told, f"the journal carried the {kind}'s old title"

	renames = [
		(entry["entity_type"], change)
		for entry in entries
		if entry["action"] == "updated"
		for change in entry["changed"]
		if change["field"] == "title"
	]

	assert sorted(kind for kind, _change in renames) == ["document", "project", "task"], renames
	assert all(
		(change["said"], change["before"], change["after"], change["quoted"])
		== ("title", None, None, False)
		for _kind, change in renames
	), renames

	# **The audit log keeps both**, so this is the words left out of a line and not the change.
	feed = world.call("GET", "/v1/changes", params={"limit": 200})

	assert feed.status_code == 200, feed.text
	assert all(before in feed.text for before, _after in titles.values()), "a title was lost"


def test_a_repeat_is_its_rule_and_never_the_item_holding_it (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""Decision `#2823`: *repeats: never to every Monday*, not *how it repeats: nothing to #4*.

	**Measured before it was built**: making an ordinary task repeat attaches it to a series, a
	hidden item of its own, and the journal named that item's number — a ref nobody is shown and
	nothing a reader could open, in place of the rule the change was.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Feed the fish"})

	assert created.status_code == 201, created.text

	ref = created.json()["ref"]
	answered = world.call("PATCH", f"/v1/tasks/{ref}", json={"recurrence": "every monday"})

	assert answered.status_code == 200, answered.text

	lines = [change for entry in _updated_lines(world, session, ref) for change in entry.changed]

	assert [change.said for change in lines] == ["repeats"], lines

	(repeat,) = lines

	assert repeat.before is None and repeat.empty == "never", repeat
	assert repeat.after and "monday" in repeat.after.lower(), repeat
	assert "#" not in repeat.after, f"a repeat named the item holding it: {repeat}"


def test_changing_how_something_repeats_is_an_entry_and_leaves_the_last_one_as_it_was (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#2825`: every Monday, every Tuesday from now on, then stopped - three entries, as they were.

	**Measured before it was fixed**: the second change wrote nothing on either item and moved no
	version, because the rule lives on the hidden series and the item compared equal to itself -
	and the first entry, which read the series' rule as it stood *now*, came to say Tuesday.
	Stopping recorded nothing either, for the same reason.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Feed the fish"})

	assert created.status_code == 201, created.text

	ref = created.json()["ref"]
	versions = [created.json()["version"]]

	for step in (
		{"recurrence": "every monday"},
		{"recurrence": "every tuesday", "applies_to": "from_now_on"},
		{"recurrence": None},
	):
		answered = world.call("PATCH", f"/v1/tasks/{ref}", json=step)

		assert answered.status_code == 200, answered.text

		versions.append(answered.json()["version"])

	assert versions == sorted(set(versions)), (
		f"a change to how it repeats left the version where it was: {versions}"
	)

	lines = [
		subroutine.views.change_in_words(change)
		for entry in _updated_lines(world, session, ref)
		for change in entry.changed
	]

	assert lines == [
		"repeats: never to every Monday",
		"repeats: every Monday to every Tuesday",
		"repeats: every Tuesday to never",
	], lines


def test_a_repeats_hidden_series_is_left_out_of_the_journal (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#2849`, Simon 2026-09-20: one act by a person reads as one entry.

	Giving an item a repeat makes a second row to hold the rule - in no listing, reachable only
	by a number nobody was shown - and the journal printed its creation above the change that
	made it: *created #2 Water the plants*, then *updated #1 Water the plants*.

	**Both halves are asserted**, because leaving the row out must not take the act with it: the
	entry against the visible item is `SR#2825`'s and stays, and the events themselves are
	untouched, so the feed a client polls still carries the series. That is the split `SR#1429`
	made between what happened and what changed.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Water the plants"})

	assert created.status_code == 201, created.text

	ref = created.json()["ref"]
	answered = world.call("PATCH", f"/v1/tasks/{ref}", json={"recurrence": "every monday"})

	assert answered.status_code == 200, answered.text

	session.flush()
	_settled(session)

	model = subroutine.db.models.work.Task
	series = session.scalars(
		sqlalchemy.select(model).where(
			model.is_template.is_(True), model.title == "Water the plants"
		)
	).one()
	entries = _entries(world, limit=200)
	refs = {entry["item_ref"] for entry in entries}

	assert series.ref != ref, "the series row is the item itself, so this proves nothing"
	assert series.ref not in refs, (
		f"the journal named the hidden series #{series.ref}: "
		f"{[entry for entry in entries if entry['item_ref'] == series.ref]}"
	)

	# **The act is still there**, on the item somebody can reach.
	assert ref in refs, f"the change to the item itself went missing with it: {refs}"

	# **And the events are untouched**, which is what keeps this a reading rather than a write.
	changed = world.call("GET", "/v1/changes", params={"limit": 200})

	assert changed.status_code == 200, changed.text
	assert str(series.id) in json.dumps(changed.json()), (
		"the series is gone from the feed a client polls, not only from the journal"
	)


def test_a_journal_page_asks_about_each_events_own_task (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3159`, L-8 of the cold review of 2026-09-21: every page read the whole task table.

	**Asserted on the statement's shape, not on a query plan**, which is the part that holds on
	both backends: a planner may scan a table this small whatever the index. The leaving-out
	itself is the test above's; this is that it asks one task per event, correlated on the
	event's own id, rather than listing every template there is.
	"""

	written = str(
		subroutine.domain.journal._not_a_rule_bearing_row().compile(
			dialect=session.get_bind().dialect
		)
	)

	assert "EXISTS" in written.upper(), written
	assert "event.entity_id" in written, f"the lookup is not correlated on the event: {written}"
	assert " IN (" not in written.upper(), f"every template is still listed: {written}"


def test_a_fact_is_named_once_however_many_columns_it_moved () -> None:
	"""Decision `#2823`'s *one line per fact*, in the feed's half: names, deduplicated and folded."""

	assert subroutine.views.fields_in_words({"status_id", "completed_at"}) == ["status"]
	assert subroutine.views.fields_in_words({"due_at", "due_is_all_day"}) == ["deadline"]
	assert subroutine.views.fields_in_words({"assignee_id", "assigned_by_id"}) == ["assignee"]
	assert subroutine.views.fields_in_words({"snoozed_until", "snoozed_is_all_day"}) == [
		"deferred until"
	]
	# Alone, each still says what moved.
	assert subroutine.views.fields_in_words({"assigned_by_id"}) == ["assigned by"]
	assert subroutine.views.fields_in_words({"completed_at"}) == ["completed"]
