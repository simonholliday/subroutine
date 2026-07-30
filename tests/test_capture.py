"""Tests for the quick-capture grammar, and above all for what it must *not* do.

No database fixture anywhere in this file, deliberately: :func:`parse` is a pure function
of text, a clock and a timezone, and that is what makes the preview path honest. A test
that needed a session would mean the preview could not be trusted to match the create.

The losslessness tests are the point. A grammar that occasionally invents a due date and
deletes the evidence from the title is worse than no grammar at all, so the cases that
would do that are enumerated here — each one measured, and each one having actually failed
before the sigil rules were tightened.
"""

import datetime

import hypothesis
import hypothesis.strategies
import pytest

import subroutine.domain.capture
import subroutine.domain.dates

#: Thursday 30 July 2026. Sunday is the 2nd of August; next Friday is the 7th.
NOW = datetime.datetime(2026, 7, 30, 14, 0, tzinfo=datetime.UTC)

LONDON = "Europe/London"


def _parse (text: str) -> subroutine.domain.capture.Capture:
	"""Parse against this file's fixed clock."""

	return subroutine.domain.capture.parse(text, now=NOW, timezone=LONDON)


def test_the_specifications_own_example_yields_its_five_fields () -> None:
	"""SPEC.md §6.13's worked example, and half of S2-03's done-criterion."""

	captured = _parse("Call the dentist before Sunday !3 ~15m #health")

	assert captured.title == "Call the dentist"
	assert captured.due == datetime.date(2026, 8, 2)
	assert captured.due_is_all_day
	assert captured.importance == 3
	assert captured.estimate_minutes == 15
	assert captured.tags == ("health",)


def test_a_sentence_with_no_grammar_in_it_is_left_entirely_alone () -> None:
	"""The other half: ``Email Bob re: 3pm`` must not become a due date.

	It survives because the grammar requires a keyword — there is no rule that looks at a
	bare time of day and hopes. Punctuation and capitalisation come through untouched.
	"""

	assert _parse("Email Bob re: 3pm").title == "Email Bob re: 3pm"


@pytest.mark.parametrize(
	("text", "title", "expected"),
	[
		("Ship it by friday", "Ship it", {"due": datetime.date(2026, 7, 31)}),
		("Ship it due sat", "Ship it", {"due": datetime.date(2026, 8, 1)}),
		("Ship it before next friday", "Ship it", {"due": datetime.date(2026, 8, 7)}),
		("Ship it by 2026-12-25", "Ship it", {"due": "2026-12-25"}),
		("Ship it by end_of_week", "Ship it", {"due": "end_of_week"}),
		("Ship it by now+3d", "Ship it", {"due": "now+3d"}),
		("Look at it on monday", "Look at it", {"planned_for": datetime.date(2026, 8, 3)}),
		("Look at it today", "Look at it", {"planned_for": datetime.date(2026, 7, 30)}),
		("Look at it tomorrow", "Look at it", {"planned_for": datetime.date(2026, 7, 31)}),
		("Renew it from monday", "Renew it", {"start": datetime.date(2026, 8, 3)}),
		("Renew it defer 2026-09-01", "Renew it", {"start": "2026-09-01"}),
		("Tidy up #home #admin", "Tidy up", {"tags": ("home", "admin")}),
		("Review the PR @si", "Review the PR", {"assignee": "si"}),
		("Fix the build +WEB", "Fix the build", {"project_key": "WEB"}),
		("Fix the build +web", "Fix the build", {"project_key": "WEB"}),
		("Write it up ~2h", "Write it up", {"estimate_minutes": 120}),
		("Write it up ~1h30m", "Write it up", {"estimate_minutes": 90}),
		("Deal with it !5", "Deal with it", {"importance": 5}),
	],
)
def test_every_token_in_the_published_grammar (
	text: str, title: str, expected: dict[str, object]
) -> None:
	"""SPEC.md §6.13's table, one row at a time."""

	captured = _parse(text)

	assert captured.title == title

	for field, value in expected.items():
		assert getattr(captured, field) == value, field


@pytest.mark.parametrize(
	"text",
	[
		# Each of these lost data before the sigil rules required a word boundary, a
		# leading letter on a tag, or a unit on an estimate.
		"Email bob@example.com about it",
		"Fix issue #12",
		"Invite ~5 people to the review",
		"C++ refactor",
		"Costs 1+1 pounds",
		"Read chapter 3.1",
		"Buy milk ~soon",
		"Ask about the C# port",
		"Reply to it!",
		"Rate it 4/5",
		# A keyword with nothing the vocabulary recognises after it stays whole.
		"Finish before the meeting",
		"Call them on the phone",
		"Take it from the top",
		"Due diligence on the contract",
	],
)
def test_text_that_does_not_parse_is_returned_verbatim (text: str) -> None:
	"""Rule 1, on every input that has ever broken it.

	The failure mode this forbids is not "the grammar missed something" — it is a task
	created with a wrong date *and* a title with the evidence deleted, which nobody can
	diagnose after the fact.
	"""

	assert _parse(text).title == text


def test_recurrence_is_recognised_only_well_enough_to_be_left_alone () -> None:
	"""``every …`` waits for M7, and until then stays in the title (SPEC.md §6.13).

	It is matched, but only to reserve the span so that ``every monday`` is not read as a
	planned day. The words themselves are untouched, and the caller is told why.
	"""

	captured = _parse("Water the plants every monday")

	assert captured.title == "Water the plants every monday"
	assert captured.planned_for is None
	assert captured.unparsed == ("every monday",)


def test_a_recurring_phrase_does_not_swallow_a_real_date () -> None:
	"""Reserving the recurrence span must not cost the deadline beside it."""

	captured = _parse("Water the plants every monday by friday")

	assert captured.due == datetime.date(2026, 7, 31)
	assert captured.title == "Water the plants every monday"


def test_a_weekday_today_means_today () -> None:
	""""By Thursday" said on a Thursday is today — the other reading makes it unsayable."""

	assert _parse("Ship it by thursday").due == datetime.date(2026, 7, 30)


def test_the_first_value_for_a_field_wins () -> None:
	"""Two deadlines in one line is a typo, and the later one does not silently replace."""

	captured = _parse("Ship it by friday by monday")

	assert captured.due == datetime.date(2026, 7, 31)


def test_everything_at_once () -> None:
	"""The whole grammar in one line, which is how it will actually be used."""

	captured = _parse("Ship the release by next friday +WEB @si ~2h #release #urgent !4")

	assert captured.title == "Ship the release"
	assert captured.due == datetime.date(2026, 8, 7)
	assert captured.project_key == "WEB"
	assert captured.assignee == "si"
	assert captured.estimate_minutes == 120
	assert captured.tags == ("release", "urgent")
	assert captured.importance == 4


def test_a_deadline_and_a_defer_can_both_be_given () -> None:
	"""Different keywords, different fields, no interference."""

	captured = _parse("Renew the passport from 2026-09-01 due 2026-10-01")

	assert captured.title == "Renew the passport"
	assert captured.start == "2026-09-01"
	assert captured.due == "2026-10-01"


def test_whitespace_left_by_a_removal_is_closed_up () -> None:
	"""Removing a token from the middle must not leave a double space behind."""

	assert _parse("Call   the  dentist #health today").title == "Call the dentist"


def test_a_line_that_is_only_grammar_leaves_an_empty_title () -> None:
	"""Refusing it is the service layer's job — `text.require` already says so.

	Parsing returns what it found, including nothing. Deciding that a task needs a title
	belongs where every other title is checked, not in two places that could disagree.
	"""

	assert _parse("#health !3").title == ""


@hypothesis.given(
	hypothesis.strategies.lists(
		hypothesis.strategies.text(
			alphabet=hypothesis.strategies.characters(
				min_codepoint=97, max_codepoint=122
			),
			min_size=1,
			max_size=8,
		),
		min_size=1,
		max_size=12,
	)
)
def test_ordinary_words_always_survive_intact (words: list[str]) -> None:
	"""Text built only of lower-case words comes back unchanged, unless it *is* grammar.

	The generated alphabet cannot produce a sigil, so the only way a word disappears is by
	being a date keyword — which is a real behaviour, not a bug, so those runs are skipped
	rather than asserted against.
	"""

	vocabulary = (
		set(subroutine.domain.capture.WEEKDAYS)
		| set(subroutine.domain.dates.KEYWORDS)
		| set(subroutine.domain.capture.BARE_PLANNED_WORDS)
		| set(subroutine.domain.capture.DEADLINE_WORDS)
		| set(subroutine.domain.capture.PLANNED_WORDS)
		| set(subroutine.domain.capture.DEFER_WORDS)
		| {"next", "every"}
	)

	hypothesis.assume(not vocabulary & set(words))

	text = " ".join(words)

	assert _parse(text).title == text


#: Realistic task lines, and the fragments that have broken this grammar before. The
#: cartesian product of these is what the invariants below run against.
_VERBS = ("Call", "Email", "Review", "Fix", "Buy", "Read", "Ask", "Book", "Renew", "Pay")
_NOUNS = (
	"the dentist", "Bob", "the report", "issue 12", "the C# port", "milk",
	"chapter 3.1", "the C++ build", "the invoice", "4/5 stars", "1+1 pounds",
)
_TAILS = (
	"", "!", "?", " re: 3pm", " -- urgent", " (again)", ", then relax",
	" before the meeting", " on the phone", " from the top", " every day",
	" at ~5 people", " for @home use", " #1 priority", " due diligence",
	" tomorrow's party", " today's news", " tomorrow-ish", " by hand", " on time",
	" #tag, and more", " @bob, and Sue", " !3, not !4", " ~2h, then rest",
)


def _words (text: str) -> list[str]:
	"""Split on whitespace the way the title is normalised."""

	import re

	return re.sub(r"\s+", " ", text).strip().split()


def _generated () -> list[str]:
	"""Return every combination of the fragments above."""

	import itertools

	return [f"{verb} {noun}{tail}" for verb, noun, tail in itertools.product(_VERBS, _NOUNS, _TAILS)]


def test_a_title_never_contains_a_word_the_input_did_not () -> None:
	"""The invariant that would have caught the possessive bug, on 2,530 generated lines.

	``tomorrow's party`` used to yield a title of ``'s party``: ``\\b`` sits between ``w``
	and ``'``, so the match tore the word in half. The losslessness table did not catch it
	because none of its fourteen strings had a possessive, and the earlier property test did
	not either, because its invariant was "a word may only vanish if a field was set" — and
	here one was.

	This states the other half: **a word may not appear in the title unless the input had
	it**. Any parse that cuts a word produces a fragment the input never contained.
	"""

	offenders = []

	for text in _generated():
		captured = _parse(text)
		original = set(_words(text))

		for word in _words(captured.title):
			if word not in original:
				offenders.append((text, captured.title, word))

	assert offenders == [], f"{len(offenders)} mangled titles, first: {offenders[:3]}"


def test_a_word_vanishes_only_when_something_was_parsed () -> None:
	"""The complementary invariant: nothing is dropped silently.

	If no field was set, the title must be the input with only its whitespace normalised.
	"""

	offenders = []

	for text in _generated():
		captured = _parse(text)

		parsed_anything = any(
			(
				captured.due,
				captured.planned_for,
				captured.start,
				captured.importance,
				captured.urgency,
				captured.estimate_minutes,
				captured.tags,
				captured.assignee,
				captured.project_key,
			)
		)

		if not parsed_anything and _words(captured.title) != _words(text):
			offenders.append((text, captured.title))

	assert offenders == [], f"{len(offenders)} silent losses, first: {offenders[:3]}"


@pytest.mark.parametrize(
	("text", "title", "expected"),
	[
		# Trailing punctuation belongs to the sentence, not to the value beside it.
		("Note the #hashtag, then move on", "Note the then move on", {"tags": ("hashtag",)}),
		("Ping @bob, then talk", "Ping then talk", {"assignee": "bob"}),
		("Write it up ~2h, then rest", "Write it up then rest", {"estimate_minutes": 120}),
		("Fix the build +WEB, please", "Fix the build please", {"project_key": "WEB"}),
		# …which also restores first-wins for importance: `!3,` used to fail to match at
		# all, letting the later `!4` win.
		("Set it to !3, not !4", "Set it to not !4", {"importance": 3}),
		# `#a #b #a` is one person typing quickly, not three tags.
		("Tag it #a #b #a", "Tag it", {"tags": ("a", "b")}),
	],
)
def test_punctuation_beside_a_sigil_is_not_part_of_its_value (
	text: str, title: str, expected: dict[str, object]
) -> None:
	"""``#hashtag,`` created a tag named "hashtag," — permanent litter, since tags auto-create."""

	captured = _parse(text)

	assert captured.title == title

	for field, value in expected.items():
		assert getattr(captured, field) == value, field


@pytest.mark.parametrize(
	("text", "title", "tags"),
	[
		# All digits: a reference, so it stays in the title for the mention index (§6.15).
		("Fix issue #12", "Fix issue #12", ()),
		("Fix #12 and #13", "Fix #12 and #13", ()),
		# The cost of the rule, stated rather than hidden: somebody wanting a tag for IEEE
		# 802.11 cannot have `#80211`, because that is how item 80211 is written. `#wifi`,
		# or `#ieee-80211`. There is no way to have both and keep them apart.
		("Read the #80211 spec", "Read the #80211 spec", ()),
		# Not all digits: an ordinary tag, even when it starts with one. These were refused
		# outright while the rule was "a tag begins with a letter", and refused *silently* —
		# the text stayed in the title and no tag was made.
		("Print the bracket #3d-printing", "Print the bracket", ("3d-printing",)),
		("Turn on #2fa", "Turn on", ("2fa",)),
		# Both in one line, each read as what it is.
		("Fix #12 for #2fa", "Fix #12 for", ("2fa",)),
	],
)
def test_a_hash_is_a_tag_unless_it_is_entirely_digits (
	text: str, title: str, tags: tuple[str, ...]
) -> None:
	"""``#`` means two things, and this is the whole of what separates them.

	A tag and a reference share the sigil (§6.13, §6.15). The rule is that a reference is
	*all* digits and a tag is anything else — not "a tag starts with a letter", which is
	stronger than it needs to be and loses ``#3d-printing`` to no purpose.
	"""

	captured = _parse(text)

	assert captured.title == title
	assert captured.tags == tags


@pytest.mark.parametrize(
	("text", "planned"),
	[
		# Last token: a plan.
		("Buy milk tomorrow", datetime.date(2026, 7, 31)),
		("Buy milk today", datetime.date(2026, 7, 30)),
		("Buy milk tomorrow.", datetime.date(2026, 7, 31)),
		# Last *once the sigils are gone*: still a plan. Refined 2026-07-29 after the API
		# work found `Renew the domain tomorrow !4` keeping the word and setting no date.
		("Buy milk tomorrow !3", datetime.date(2026, 7, 31)),
		("Buy milk tomorrow #shopping", datetime.date(2026, 7, 31)),
		("Buy milk tomorrow ~20m @alice !2", datetime.date(2026, 7, 31)),
		("Buy milk !3 tomorrow", datetime.date(2026, 7, 31)),
		# Anywhere else: prose.
		("Remember what happened today, then write it up", None),
		("Ask about tomorrow-ish plans", None),
		("Buy a present for tomorrow's party", None),
		("Today I will rest", None),
		("Buy milk tomorrow and bread", None),
	],
)
def test_a_bare_day_plans_only_at_the_end_of_the_line (
	text: str, planned: datetime.date | None
) -> None:
	"""Settled 2026-07-29: bare ``today``/``tomorrow`` plan only as the final token.

	Mid-sentence they are almost always prose, and reading one as a field both sets a date
	nobody asked for and takes a word out of the title. At the end of the line —
	"buy milk tomorrow" — the reading is unambiguous and it is how people write.

	**Refined the same day: last means last once the sigils are removed.** Read against the
	raw line, the rule also caught ``Buy milk tomorrow !3``, where the only thing following
	the word is a token being taken out of the title anyway. The protection is unchanged —
	what it exists to stop is a bare day inside *prose*, and a trailing ``!3`` is not prose.
	"""

	assert _parse(text).planned_for == planned


def test_an_unparsed_recurrence_still_counts_as_words_after_a_bare_day () -> None:
	"""``every monday`` stays in the title (M7), so a ``tomorrow`` before it is mid-sentence.

	This is the edge the sigil refinement had to not break: a claimed span is one that
	*leaves* the title, and a reserved one is not.
	"""

	captured = _parse("Do it tomorrow every monday")

	assert captured.planned_for is None
	assert captured.title == "Do it tomorrow every monday"


def test_a_captured_line_can_set_both_priority_axes () -> None:
	"""SPEC.md §6.3 has two axes and this grammar reached one until 2026-07-30.

	``!4`` alone is not a smaller version of ``!4/2`` — it is a *worse* one. ``priority_score``
	is null unless both axes are set and every ordering is NULLS LAST, so a task captured
	``!4`` scored null and sank below everything ranked, looking exactly like something judged
	unimportant. Anybody typing ``!4`` reached that; it was not a corner case.

	Spelled the way a listing renders it back, so what you read is what you can type.
	"""

	captured = _parse("Fix the boiler !4/2")

	assert captured.title == "Fix the boiler"
	assert (captured.importance, captured.urgency) == (4, 2)


def test_an_importance_on_its_own_still_works_and_leaves_urgency_unset () -> None:
	"""The older spelling is unchanged, and does not invent the axis it was not given.

	Defaulting the missing axis here would be a guess wearing a convenience's clothes — and
	it would put a number nobody chose into the ordering everything is ranked by.
	"""

	captured = _parse("Fix the boiler !4")

	assert (captured.importance, captured.urgency) == (4, None)


@pytest.mark.parametrize(
	"text",
	[
		"Reduce it by !4/9 percent",
		"Ratio is !0/2 here",
		"Scale !6/2 up",
	],
)
def test_a_pair_outside_the_range_is_left_in_the_title (text: str) -> None:
	"""Out of range is not grammar, so §6.13 rule 1 applies: it stays in the title verbatim.

	The failure this guards is the one the date tokens already caused once — a pattern that
	half-matches, claims part of the span, and leaves a mangled title behind.
	"""

	captured = _parse(text)

	assert captured.urgency is None
	assert _words(captured.title) == _words(text), "part of the text was eaten"
