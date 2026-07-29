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
