"""Tests for the quick-capture grammar, and above all for what it must *not* do.

No database fixture anywhere in this file, deliberately: :func:`parse` is a pure function
of text, a clock and a timezone, and that is what makes the preview path honest. A test
that needed a session would mean the preview could not be trusted to match the create.

The losslessness tests are the point. A grammar that occasionally invents a due date and
deletes the evidence from the title is worse than no grammar at all, so the cases that
would do that are enumerated here — each one measured, and each one having actually failed
before the sigil rules were tightened.
"""

import dataclasses
import datetime

import hypothesis
import hypothesis.strategies
import pytest

import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.projects
import subroutine.domain.recurrence

#: Thursday 30 July 2026. Sunday is the 2nd of August; next Friday is the 7th.
NOW = datetime.datetime(2026, 7, 30, 14, 0, tzinfo=datetime.UTC)

LONDON = "Europe/London"


def _parse (text: str) -> subroutine.domain.capture.Capture:
	"""Parse against this file's fixed clock."""

	return subroutine.domain.capture.parse(text, now=NOW, timezone=LONDON)


def test_the_specifications_own_example_yields_its_five_fields () -> None:
	"""docs/design.md §6.13's worked example, and half of S2-03's done-criterion."""

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
		("Look at it on monday", "Look at it", {"starts_at": datetime.date(2026, 8, 3)}),
		("Look at it today", "Look at it", {"starts_at": datetime.date(2026, 7, 30)}),
		("Look at it tomorrow", "Look at it", {"starts_at": datetime.date(2026, 7, 31)}),
		("Renew it from monday", "Renew it", {"snooze": datetime.date(2026, 8, 3)}),
		("Renew it defer 2026-09-01", "Renew it", {"snooze": "2026-09-01"}),
		("Tidy up #home #admin", "Tidy up", {"tags": ("home", "admin")}),
		("Review the PR @si", "Review the PR", {"assignee": "si"}),
		("Fix the build +web", "Fix the build", {"project_key": "web"}),
		# **Read as written since `#508`.** The grammar reports what somebody typed and
		# `projects.normalize_key` decides the stored form — one copy of that rule, in
		# the service. It used to be here as well, and the two disagreed the moment the
		# rule changed.
		("Fix the build +web", "Fix the build", {"project_key": "web"}),
		("Write it up ~2h", "Write it up", {"estimate_minutes": 120}),
		("Write it up ~1h30m", "Write it up", {"estimate_minutes": 90}),
		("Deal with it !5", "Deal with it", {"importance": 5}),
	],
)
def test_every_token_in_the_published_grammar (
	text: str, title: str, expected: dict[str, object]
) -> None:
	"""docs/design.md §6.13's table, one row at a time."""

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


def test_a_repeat_is_read_off_the_line_and_leaves_the_title (  ) -> None:
	"""``every …`` is parsed since `#94`, and the words go because a field was set.

	**This test used to assert the opposite** — that the phrase was reserved and left in the
	title — and the reservation was never about the words: it stopped ``every monday`` being
	read as a planned day while the rule could not be stored. Now that it can, §6.13's rule
	applies the other way round: a word may vanish exactly when a field was set, and one was.

	The span is still claimed *first*, and that is unchanged and still load-bearing: ``on`` and
	``monday`` both belong to the date grammar, so anything reading the line before this would
	take half a repeat and set a date from it.
	"""

	captured = _parse("Water the plants every monday")

	assert captured.title == "Water the plants"
	assert captured.recurrence == "FREQ=WEEKLY;BYDAY=MO"
	assert captured.recurrence_text == "every monday"
	assert captured.starts_at is None
	assert captured.unparsed == ()


def test_the_preview_quotes_the_recurrence_a_person_actually_wrote () -> None:
	"""`#206`. It quoted a fragment, on the one surface whose job is confirming their words.

	``every\\s+\\S+`` stopped at the first token, so "Water plants every 2 days" reported
	*"Left as written: every 2"* — a sentence about what somebody typed that misquotes them.
	The title was always right; the report of it was not, which is the half §6.13 rule 1
	cannot check for itself because the words are all still there.

	A count and ``other`` are the two things that come between ``every`` and its unit, so they
	are what the pattern has to reach past before it can quote anything.

	**Every line here is one the grammar still cannot read** — `#94` made the readable ones a
	rule, so quoting them back is no longer the question asked of them. What survives is the
	obligation for the rest: the words stay, and they are quoted whole.
	"""

	for line, phrase in (
		("Review the budget every fortnight", "every fortnight"),
		("Water plants every sausages", "every sausages"),
		("Bins out every 0 days", "every 0 days"),
	):
		captured = _parse(line)

		assert captured.title == line, "rule 1: the words are untouched either way"
		assert captured.unparsed == (phrase,), f"{line!r} was quoted back as {captured.unparsed}"

		explained = subroutine.domain.capture.explain(captured.unparsed)

		assert explained is not None
		assert phrase in explained

	# And a word that merely starts with "every" is not a recurrence at all.
	assert _parse("Ask everyone about it").unparsed == ()


def test_a_date_word_inside_another_word_is_not_a_date_word () -> None:
	"""`#929`. ``\\b`` is not "a word starts here", and ``_DATED`` was the one pattern using it.

	**Both edges were wrong and each loses a word**, which is §6.13 rule 1's forbidden outcome:

	- at the front it matched *inside* a hyphenated word, so ``add-on`` gave up its ``on`` and
	  the task was filed as ``Ship the add-``;
	- at the back it matched before an apostrophe, so ``by tomorrow's deadline`` filed
	  ``Ship it 's deadline``.

	The second is the defect this module already records as the reason ``_BARE_DAY`` is
	anchored — written down, and fixed in one pattern of the two it was true of.
	"""

	for line in (
		"Ship the add-on tomorrow",
		"Fix the stand-by wednesday",
		"Review the sign-on flow on monday",
		"Ship it by tomorrow's deadline",
	):
		captured = _parse(line)

		# Rule 1 is about *words*, so the assertion is about words rather than about the
		# whole title: `on monday` above is a real date and correctly leaves the title.
		for word in ("add-on", "stand-by", "sign-on", "tomorrow's"):
			if word in line:
				assert word in captured.title, f"{line!r} lost {word!r}, filing {captured.title!r}"

	# Falsifying the fix must not cost the dates that made it worth having, so the ordinary
	# forms are asserted here rather than trusted to another test. `on` plans a day and `by`
	# sets a deadline, which is why the two are read off different fields.
	assert _parse("Call the dentist on monday").starts_at is not None
	assert _parse("Pay the rent by 2026-08-19.").due is not None


def test_a_time_given_back_is_not_reported_as_a_failed_repeat () -> None:
	"""`#929`. Three things reach ``unparsed`` and ``explain`` sorted them into two.

	A ``+`` nobody could parse, a repeat phrased in a way the grammar does not know, **and a
	time this module read and deliberately gave back** — the third being reported as the
	second. So ``Email Bob re: 3pm``, which is ``explain capture``'s own worked example and
	whose documented point is that none of it is grammar, answered *"not a repeat this
	understands"*.
	"""

	timed = _parse("Email Bob re: 3pm")

	assert timed.title == "Email Bob re: 3pm", "rule 1: the words are untouched"
	assert timed.unparsed == ("3pm",)

	about_a_time = subroutine.domain.capture.explain(timed.unparsed)

	assert about_a_time is not None
	assert "repeat" not in about_a_time, about_a_time
	assert "at" in about_a_time

	# The other half: a real repeat attempt must still be told it is one, or this is simply a
	# check that the sentence stopped being said.
	about_a_repeat = subroutine.domain.capture.explain(
		_parse("Water plants every fortnight").unparsed
	)

	assert about_a_repeat is not None
	assert "not a repeat this understands" in about_a_repeat


def test_a_project_name_the_grammar_cannot_read_is_reported (  ) -> None:
	"""`SR#778`, Simon 2026-08-10, reading a title in the browser.

	The same mistake had three different answers, and the best and worst were next to each
	other. Measured on a disposable instance before this was written:

	| typed | what happened |
	| --- | --- |
	| `+inbox` — exists | consumed and filed there |
	| `+nosuchproject` — a key shape, no such project | **refused by name**, listing the real ones |
	| `+subroutine/UI` — not a key shape at all | **silently left in the title**, filed at the default |

	`_PROJECT` could not read past the slash, so the token was ordinary prose and nothing
	noticed that a `+` went unclaimed. **Eight items were filed into the wrong project
	believing otherwise**, and the titles carried the junk until somebody read a list.

	§6.13 rule 1 is *the words stay and the caller is told*; this was the half without the
	telling.

	**The original example is now read in full** (decision `#957` made a slash an address), so
	the shape here is an underscore, which is in no key and will not become one — the point
	being the rule rather than the character. That the worked example moved is what this test
	is *for*: the next widening leaves a different unreadable name behind, and the report has
	to keep finding it.
	"""

	captured = _parse("Fix the header +web_sales")

	assert captured.title == "Fix the header +web_sales", "rule 1: the words are untouched"
	assert captured.project_key is None, "an unreadable name must not be guessed at"
	assert captured.unparsed == ("+web_sales",)

	explained = subroutine.domain.capture.explain(captured.unparsed)

	assert explained is not None and "+web_sales" in explained
	assert "project" in explained, "the sentence does not say what kind of thing was not read"

	# **An address is read now, and reported by nothing.** The row above this one in the table
	# is what changed; falsifying `_PROJECT` back to a single key fails here.
	address = _parse("Fix the header +subroutine/ui")

	assert address.project_key == "subroutine/ui"
	assert address.unparsed == ()
	assert address.title == "Fix the header"

	# **A name the rules did read is not reported**, or every capture would carry a complaint.
	assert _parse("Fix the header +web").unparsed == ()

	# **Both kinds at once get both reasons**, which is why the sentence is built per kind
	# rather than being one string with one ending.
	#
	# **The repeat is last on the line and that is required since `#1408`**: an unreadable
	# repeat is reported only where nothing unclaimed follows it, and an unreadable `+name`
	# is itself unclaimed. Written the other way round — `every fortnight +a_b` — this line
	# now says only the project half, correctly.
	both = subroutine.domain.capture.explain(_parse("Bins out +a_b every fortnight").unparsed)

	assert both is not None
	assert "repeat" in both and "project" in both


def test_a_plus_inside_a_word_is_not_a_project_name () -> None:
	r"""The bound on `SR#778`, without which the fix is *complain about every plus sign*.

	**Safe by construction rather than by an exclusion list**: `_STARTS_A_WORD` is
	`(?<![^\s])`, so a `+` has to begin a word, and a bare one between spaces has no `\S`
	after it to match. That is the whole guard, and these are the cases it has to survive.
	"""

	for line in ("C++ is fine", "maths a+b holds", "one plus one is 1 + 1", "a trailing plus +"):
		captured = _parse(line)

		assert captured.unparsed == (), f"{line!r} was reported as a project name"
		assert captured.title == line, f"{line!r} lost a word"


@pytest.mark.parametrize(
	"line",
	[
		"Call +44 7911 123456",
		"Buy a +1 adapter",
		"Order +2 more chairs",
		"Ring mum +447911123456 tomorrow",
	],
)
def test_a_number_after_a_plus_is_not_a_project_name (line: str) -> None:
	"""`SR#790`, found reviewing `SR#778` — the fix for silence over-fired into noise.

	The pattern was `\\+\\S+`, so every `+` beginning a word was reported *with a sentence about
	project names*: **"Call +44 7911 123456"** was answered with *a project is named like
	'+web'*. The item is filed correctly and the words stay in the title either way, so nothing
	was lost but the sentence — and a sentence that misdescribes what happened is the failure
	§6.13 rule 1 exists to prevent, arriving from the side meant to fix it.

	A phone number in a to-do list is not an exotic input, and this was silent before `SR#778`.
	"""

	captured = _parse(line)
	number = next(word for word in line.split() if word.startswith("+"))

	assert captured.unparsed == (), f"{line!r} was explained as a broken project name"

	# **The number, not the whole line.** *"Ring mum … tomorrow"* legitimately loses its date to
	# the grammar, and asserting on the whole string made this test about date parsing — which
	# it is not, and which is already covered.
	assert number in captured.title, f"{line!r} lost the number itself"
	assert captured.project_key is None, "a number was read as a project"


def test_what_can_be_reported_is_what_could_have_been_a_key () -> None:
	"""`SR#790`. The bound is derived from the key rule rather than from the noise it removes.

	**A project key begins with a letter** — `projects.KEY_PATTERN` is `[a-z][a-z0-9]*…` and
	input is case-folded by `normalize_key` before it is checked — so a `+` carrying anything
	else was never an attempt at one, and reporting it explains a mistake nobody made.

	Written as an agreement between the two rules rather than as a list of characters, so a
	`KEY_PATTERN` that one day admitted something else fails here rather than making the report
	quietly narrower than the thing it describes.

	The broken tail is an underscore rather than the slash it was until `#957`, which made a
	slash the separator between keys and so made `+ax/y` a perfectly good address.
	"""

	shape = subroutine.domain.projects.KEY_PATTERN
	folded = subroutine.domain.projects.normalize_key

	for first in ("a", "z", "A", "Z"):
		assert shape.match(folded(first)), f"{first!r} no longer begins a key"
		assert _parse(f"Fix it +{first}x_y").unparsed == (f"+{first}x_y",), (
			f"a key could begin with {first!r} and a broken one starting with it is not reported"
		)

	for first in ("0", "9"):
		assert not shape.match(folded(first)), f"{first!r} now begins a key"
		assert _parse(f"Fix it +{first}x_y").unparsed == (), (
			f"no key can begin with {first!r}, so a +{first}… is not a project somebody mistyped"
		)


def test_a_recurring_phrase_does_not_swallow_a_real_date () -> None:
	"""Claiming the recurrence span must not cost the deadline beside it.

	**Sharper since `#94` widened the pattern**, because it is now greedy on purpose: it has
	to reach past ``on the 30th`` so the date grammar cannot take half a repeat, and a pattern
	that reaches too far takes a deadline that was never part of one.
	"""

	captured = _parse("Water the plants every monday by friday")

	assert captured.due == datetime.date(2026, 7, 31)
	assert captured.recurrence == "FREQ=WEEKLY;BYDAY=MO"
	assert captured.title == "Water the plants"


def test_a_weekday_today_means_today () -> None:
	""""By Thursday" said on a Thursday is today — the other reading makes it unsayable."""

	assert _parse("Ship it by thursday").due == datetime.date(2026, 7, 30)


def test_the_first_value_for_a_field_wins () -> None:
	"""Two deadlines in one line is a typo, and the later one does not silently replace."""

	captured = _parse("Ship it by friday by monday")

	assert captured.due == datetime.date(2026, 7, 31)


def test_everything_at_once () -> None:
	"""The whole grammar in one line, which is how it will actually be used."""

	captured = _parse("Ship the release by next friday +web @si ~2h #release #urgent !4")

	assert captured.title == "Ship the release"
	assert captured.due == datetime.date(2026, 8, 7)
	assert captured.project_key == "web"
	assert captured.assignee == "si"
	assert captured.estimate_minutes == 120
	assert captured.tags == ("release", "urgent")
	assert captured.importance == 4


def test_a_deadline_and_a_defer_can_both_be_given () -> None:
	"""Different keywords, different fields, no interference."""

	captured = _parse("Renew the passport from 2026-09-01 due 2026-10-01")

	assert captured.title == "Renew the passport"
	assert captured.snooze == "2026-09-01"
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
		set(subroutine.domain.dates.WEEKDAYS)
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
				captured.starts_at,
				captured.snooze,
				captured.importance,
				captured.urgency,
				captured.estimate_minutes,
				captured.tags,
				captured.assignee,
				captured.project_key,
				captured.recurrence,
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
		("Fix the build +web, please", "Fix the build please", {"project_key": "web"}),
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

	assert _parse(text).starts_at == planned


@pytest.mark.parametrize(
	("text", "repeat"),
	[
		# **The line that produced the defect**, filed on 2026-08-27 while building the
		# roadmap. It became a daily repeating task due today, in two rows, with the words
		# gone from the title.
		("A view somebody uses every day can be saved and shared", None),
		("Ask whether every month is too often", None),
		("Water the plants every 14 days on friday, then rest", None),
		# **And everything that must go on working.** Nothing unclaimed follows any of these:
		# a sigil, a deadline phrase and a time are all on their way out of the title, which
		# is `_BARE_DAY`'s own argument about a trailing `!3`.
		("Buy milk every day", "every day"),
		("Pay the rent every month !3 #home", "every month"),
		("Water the plants every 14 days by friday", "every 14 days"),
		("Water the plants every 14 days on friday", "every 14 days"),
		("Review every month on the 30th", "every month on the 30th"),
		# **A sentence that ends**, which is the reason the allowance is punctuation and not
		# only whitespace — measured, because `every 14 days.` is never read as a repeat at
		# all and the obvious probe cannot reach this.
		("Water the plants every 14 days on friday.", "every 14 days"),
	],
)
def test_a_repeat_is_read_only_where_nothing_unclaimed_follows_it (
	text: str, repeat: str | None
) -> None:
	"""`SR#1401`: §6.13's rule for a bare day, applied to the grammar that shipped after it.

	That rule is settled and written down — *a bare ``today``/``tomorrow`` plans only as the
	last token of the line, measured after the sigils are removed* — and its reason is this
	one exactly: *mid-sentence these words are almost always prose, and reading one as a field
	both sets a date nobody asked for and takes a word out of the title*. ``every …`` was M7
	and never inherited it.

	**Worse than a mangled title, because a repeat is two rows** (`SR#1247`). One careless
	line made two items, the one shown was not the one that governs, and undoing it took two
	deletes in an order `SR#1294` decides.

	**The claimed/unclaimed distinction is the whole of it**, which is why the check runs
	after every other rule rather than beside the repeat pass: a deadline, a time and a sigil
	all follow a repeat legitimately, and a rule written earlier would have refused all three.
	"""

	assert _parse(text).recurrence_text == repeat


def test_a_repeat_left_mid_sentence_is_not_reported_as_unreadable () -> None:
	"""And the reason is told apart from the other one — `SR#1401`.

	A phrase this grammar cannot read and one it read out of the middle of a sentence are
	**textually identical**, so nothing about the token separates them. Offering *try 'every
	day', 'every 14 days'…* to somebody who never wanted a repeat is a refusal asserting a
	cause it has not established — and that hint is the whole content of the other message.

	:func:`subroutine.domain.capture.explain` asks :func:`_repeat_in`, which is the function
	:func:`parse` used to decide, so this is one description of what a repeat looks like
	rather than two.
	"""

	mid = _parse("A view somebody uses every day can be saved and shared")
	unreadable = _parse("Review the logs every fortnight")

	assert mid.unparsed == ("every day",)
	assert unreadable.unparsed == ("every fortnight",)

	said = subroutine.domain.capture.explain(mid.unparsed) or ""
	other = subroutine.domain.capture.explain(unreadable.unparsed) or ""

	assert "words follow it" in said, said
	assert subroutine.domain.recurrence.PHRASE_HINT not in said, (
		f"a writer who never wanted a repeat is told how to phrase one:\n{said}"
	)
	assert subroutine.domain.recurrence.PHRASE_HINT in other, other


def test_a_sentence_containing_the_word_every_is_not_told_how_to_phrase_a_repeat () -> None:
	"""`SR#1408`, Simon's decision of 2026-08-28: silent where both signals are absent.

	``_EVERY`` matches ``every\\s+\\S+`` anywhere in the line, so any sentence holding the word
	had its next word swallowed into a candidate phrase and quoted back when it did not parse.
	Filing *"Every piece of the browser's state lives in one function"* answered *"Left as
	written: Every piece — not a repeat this understands. Try 'every day'…"*, and it fired
	again on the very next line, on a title beginning *"A title beginning with the word
	Every"*, reporting ``Every is``.

	**Two signals say somebody meant a rule: the phrase's shape and its position.** `SR#1401`
	settled position for the phrases that *do* parse. Where both are absent there is nothing to
	report — the grammar took nothing, changed nothing, and every word is still in the title.

	**The advice is why this could not simply be extended.** For a readable phrase *"put it at
	the end to make it one"* is true and actionable; for an unreadable one it is false, because
	putting ``every fortnight`` at the end will not make a repeat either.

	Three cases, and the last two are what stop this passing by measuring nothing.
	"""

	for line in (
		"Every piece of the browser's state lives in one function",
		"A title beginning with the word Every is told how to phrase a repeat",
		"Every fortnight the bins go out and I always forget",
	):
		quiet = _parse(line)

		assert quiet.title == line, "rule 1: the words are untouched"
		assert quiet.recurrence is None, "nothing was read, so nothing may be set"
		assert quiet.unparsed == (), f"{line!r} was quoted back as {quiet.unparsed}"
		assert subroutine.domain.capture.explain(quiet.unparsed) is None

	# **An unreadable phrase with nothing after it is still reported**, which is the half
	# §6.13 rule 1 requires and the half a blanket silencing would take with it.
	at_the_end = _parse("Review the budget every fortnight")

	assert at_the_end.unparsed == ("every fortnight",)

	told = subroutine.domain.capture.explain(at_the_end.unparsed) or ""

	assert subroutine.domain.recurrence.PHRASE_HINT in told, told

	# **And a *readable* phrase mid-sentence keeps its own message**, which is `SR#1401` and is
	# the other thing over-silencing would destroy. Both directions of the split, in one test,
	# because a check that only proves the sentence stopped being said cannot tell this fix
	# from deleting the feature.
	readable = _parse("A view somebody uses every day can be saved and shared")

	assert readable.unparsed == ("every day",)
	assert "words follow it" in (subroutine.domain.capture.explain(readable.unparsed) or "")

	# **The boundary, met while writing this.** *"Every year the accounts have to be filed"*
	# looks like the cases above and is not one of them: ``every year`` **parses**, so it is
	# `SR#1401`'s row rather than this one and keeps its own message. What separates the two is
	# the phrase, never the position of the word in the sentence.
	parses = _parse("Every year the accounts have to be filed")

	assert parses.unparsed == ("Every year",)
	assert "words follow it" in (subroutine.domain.capture.explain(parses.unparsed) or "")


def test_an_unparsed_recurrence_still_counts_as_words_after_a_bare_day () -> None:
	"""A phrase this cannot read stays in the title, so a ``tomorrow`` before it is prose.

	**The distinction the sigil refinement had to not break, and it survived `#94`**: a claimed
	span *leaves* the title and a reserved one does not, and a bare day is only read at the end
	of what is left. ``every fortnight`` is still reserved rather than claimed, so the words
	after ``tomorrow`` are still words.
	"""

	captured = _parse("Do it tomorrow every fortnight")

	assert captured.starts_at is None
	assert captured.title == "Do it tomorrow every fortnight"

	# **And the readable one is the mirror**, which is what says the rule is about claiming
	# rather than about the word `every`: the phrase goes, so `tomorrow` is now at the end.
	read = _parse("Do it tomorrow every monday")

	assert read.title == "Do it"
	assert read.recurrence == "FREQ=WEEKLY;BYDAY=MO"

	# **`tomorrow` becomes readable because the repeat left**, which is the whole point: a bare
	# day is only read at the end of what remains, so claiming the phrase in front of it moves
	# the end. Starts tomorrow, comes back every Monday — both, and both wanted.
	assert read.starts_at == datetime.date(2026, 7, 31)


def test_a_captured_line_can_set_both_priority_axes () -> None:
	"""docs/design.md §6.3 has two axes and this grammar reached one until 2026-07-30.

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


#: How each field of :class:`~subroutine.domain.capture.Capture` reaches the user, and why
#: (`#135`). ``summarise`` is responsible for the sigils; the dates are rendered in human form
#: beside the title instead, because "(due Sun 2 Aug)" says more than echoing "by friday" back.
#: **A new field fails the classification test until it appears here**, which is the point:
#: the defect this fixes was a field that was read, stored and never mentioned, and one more
#: of those is added by writing a parser and forgetting there is an answer owed.
REPORTED_AS = {
	"project_key": "sigil",
	"importance": "sigil",
	"urgency": "sigil",
	"estimate_minutes": "sigil",
	"assignee": "sigil",
	"tags": "sigil",
	"due": "date, rendered beside the title",
	"due_is_all_day": "date, rendered beside the title",
	"starts_at": "date, rendered beside the title",
	"starts_is_all_day": "date, rendered beside the title",
	"snooze": "date, rendered beside the title",
	#: **Rendered rather than echoed**, exactly as a date is, and for the same reason: the
	#: useful confirmation is *what it turned out to mean*. `recurrence.describe` reads the
	#: stored rule back — "every month, on the 30th" — so the words come back changed, which
	#: is what lets somebody see whether the thing understood is the thing they meant.
	"recurrence": "rendered beside the title, from the rule",
	"recurrence_text": "the words as typed; the rendering above is what is shown",
	"snoozed_is_all_day": "date, rendered beside the title",
	"title": "is the title",
	"unparsed": "reported by explain(), which is this function's mirror",
}


def test_every_field_the_grammar_can_set_is_accounted_for () -> None:
	"""The guard, rather than the summary being right today.

	``+web`` was parsed, filed and never mentioned for as long as the sigil existed, and
	nothing failed — because no test asked whether a parsed field was *reported*. This is that
	question, asked of the dataclass rather than of a list somebody maintains.
	"""

	declared = {field.name for field in dataclasses.fields(subroutine.domain.capture.Capture)}
	missing = declared - set(REPORTED_AS)

	assert not missing, (
		f"{sorted(missing)} can be set by the grammar and nothing says how it is reported. "
		f"Add it to summarise(), or record in REPORTED_AS why it reaches the user elsewhere."
	)
	assert not set(REPORTED_AS) - declared, "REPORTED_AS names a field that no longer exists"


def test_a_line_with_no_sigils_summarises_to_nothing () -> None:
	"""So an ordinary "Buy milk" is answered exactly as it always was (§1.4)."""

	read = subroutine.domain.capture.parse("Buy milk", now=NOW)

	assert subroutine.domain.capture.summarise(read) is None


@pytest.mark.parametrize(
	("text", "expected"),
	[
		("Fix it +web", "+web"),
		("Think about it !3", "!3"),
		("Do it !4/2", "!4/2"),
		("Write it ~2h", "~2h"),
		("Write it ~90m", "~1h 30m"),
		("Ask her @alice", "@alice"),
		("Tidy up #home #admin", "#home #admin"),
		("Fix the header !4/2 ~2h #ops +web", "+web !4/2 ~2h #ops"),
	],
)
def test_each_sigil_is_written_back_as_it_was_typed (text: str, expected: str) -> None:
	"""Which is what makes the line need no vocabulary — it is the user's own words.

	``~90m`` becoming ``~1h 30m`` is the one that is not literally what was typed, and it is
	right: the confirmation somebody wants from a duration is what it came to.
	"""

	read = subroutine.domain.capture.parse(text, now=NOW)

	assert subroutine.domain.capture.summarise(read) == expected


def test_the_read_back_line_cannot_be_mistaken_for_the_title () -> None:
	"""Item ``#426``. The tokens alone had a double space for a separator and nothing else.

	``Added: Stop the stamp brokering an introduction  +terence !4/3 #prompt`` gives a reader
	no way to tell where the title stops — which defeats the confirmation `#135` exists for,
	since the question being answered is exactly *"was `+terence` understood, or left in the
	title?"* and both readings rendered identically.

	Reported by an agent that liked the echo and could not parse it.
	"""

	read = subroutine.domain.capture.parse("Fix the header !4/2 +web", now=NOW)
	echoed = subroutine.domain.capture.read_back(subroutine.domain.capture.summarise(read))

	assert echoed == "(read +web !4/2)"

	# The tokens themselves are untouched — this wraps `summarise`, it does not re-spell it,
	# so `--json`'s `read` field and every test above still describe the same thing.
	tokens = subroutine.domain.capture.summarise(read)

	assert tokens is not None
	assert tokens in echoed


def test_nothing_read_says_nothing_at_all () -> None:
	"""``None`` in, ``None`` out, so §1.4's "Buy milk" gains no machinery it did not ask for.

	The mirror of ``explain``'s contract, and the reason both are functions rather than
	f-strings at three call sites: an empty confirmation is a line, and a line about nothing
	is what `#135` was careful not to add.
	"""

	assert subroutine.domain.capture.read_back(None) is None


def test_the_summary_never_claims_a_field_the_grammar_did_not_set () -> None:
	"""The direction that would be worse: telling somebody it filed work somewhere it did not.

	Every sigil in the summary has to correspond to a value on the parse it came from, so a
	summary built from the wrong object — or one that guessed a default — fails here.
	"""

	read = subroutine.domain.capture.parse("Fix it +web ~2h", now=NOW)
	summary = subroutine.domain.capture.summarise(read) or ""

	assert ("!" in summary) is (read.importance is not None)
	assert ("@" in summary) is (read.assignee is not None)
	assert ("#" in summary) is bool(read.tags)
	assert ("+" in summary) is (read.project_key is not None)


#: Times this grammar reads, and where each one lands. The clock is Thursday 30 July 2026, so
#: `today` is the 30th, `tomorrow` the 31st, and `monday` the 3rd of August.
#:
#: **Parametrised over the signal rather than over the format**, because the signal is what was
#: decided: a time is read when introduced by `at`, or when it follows a date already read.
READS_A_TIME = (
	("Solar eclipse today at 18:30", "Solar eclipse", "starts_at", datetime.datetime(2026, 7, 30, 18, 30)),
	("Ship it tomorrow at 9am", "Ship it", "starts_at", datetime.datetime(2026, 7, 31, 9, 0)),
	("Call Bob at 3pm", "Call Bob", "starts_at", datetime.datetime(2026, 7, 30, 15, 0)),
	("Book table at 7:45pm today", "Book table", "starts_at", datetime.datetime(2026, 7, 30, 19, 45)),
	("Report due today at 17:00", "Report", "due", datetime.datetime(2026, 7, 30, 17, 0)),
	("Standup from monday 09:00", "Standup", "snooze", datetime.datetime(2026, 8, 3, 9, 0)),
	("Backup at 12am", "Backup", "starts_at", datetime.datetime(2026, 7, 30, 0, 0)),
	("Lunch at 12pm", "Lunch", "starts_at", datetime.datetime(2026, 7, 30, 12, 0)),
)


#: Which all-day flag belongs to which date field, since the two are not spelled alike.
ALL_DAY_FLAGS = {
	"due": "due_is_all_day",
	"starts_at": "starts_is_all_day",
	"snooze": "snoozed_is_all_day",
}


@pytest.mark.parametrize(
	("text", "title", "field", "expected"), READS_A_TIME, ids=[one[0] for one in READS_A_TIME]
)
def test_a_time_of_day_is_read_into_the_field_the_line_named (
	text: str, title: str, field: str, expected: datetime.datetime
) -> None:
	"""`#797`. A captured line could carry a day and never a time.

	**Simon met this twice** — `Dentist appointment Monday 14:00` while driving `#755`, and
	`Solar eclipse today at 18:30` in his own workspace three days later, where the whole line
	stayed in the title and nothing was set.

	**A preposition wins where there is one**, so `due … at 17:00` is a deadline and `from
	monday 09:00` is a defer; otherwise a time lands on `starts_at`. Until `#854` it landed on
	the *defer* instead, because that was the only column able to hold a clock — so every one
	of these lines filed an appointment that hid itself until it began.

	`12am` and `12pm` are here because they are the one pair a naive `hour + 12` gets wrong.
	"""

	captured = _parse(text)

	assert captured.title == title
	assert getattr(captured, field) == expected

	# **Named rather than derived from the field.** `starts_at` pairs with `starts_is_all_day`
	# and `snooze` with `snoozed_is_all_day`, so a suffix rule would have read an attribute
	# that does not exist and `getattr` would have raised where it should assert.
	assert getattr(captured, ALL_DAY_FLAGS[field]) is False
	assert captured.unparsed == ()


def test_a_time_and_a_bare_day_make_one_start_rather_than_moving_it () -> None:
	"""The decision that is easiest to get backwards, so it is asserted rather than implied.

	`Solar eclipse today` starts today, all day; `Solar eclipse today at 18:30` starts at half
	past six that evening. **One field, two precisions** — where before `#854` the day was
	popped off `planned_for` and rewritten into the defer, so adding a time to a line moved the
	fact into the column that *hides* the row.

	The guard that matters is the last one: whatever a clock does here, it must not defer.
	"""

	planned = _parse("Solar eclipse today")

	assert planned.starts_at == datetime.date(2026, 7, 30)
	assert planned.snooze is None

	timed = _parse("Solar eclipse today at 18:30")

	assert timed.starts_at == datetime.datetime(2026, 7, 30, 18, 30)
	assert timed.starts_is_all_day is False

	# **The whole point of the split.** A clock on a captured line must never hide the item.
	assert timed.snooze is None


#: Lines carrying something time-shaped that is deliberately not read, and what survives.
#:
#: **Every one keeps its title whole**, which is §6.13 rule 1 and the reason this grammar
#: refuses rather than guesses.
LEAVES_A_TIME = (
	# Prose. Guarded since the grammar existed: "there is no rule that looks at a bare time of
	# day and hopes" — `at` and adjacency are what separate a signal from a number.
	("Email Bob re: 3pm", "3pm"),
	# A bare weekday is not read, so there is no day to attach a time to. Inventing *today*
	# here set a start that contradicted the word `Monday` printed beside it.
	("Dentist appointment Monday 14:00", "14:00"),
	# **The one that reaches the give-back**, and the reason this list is not just the four
	# obvious shapes. `at` signals a time, so the span is claimed before anybody knows whether
	# it can be placed — and `Monday` is unread, so it cannot. Without giving the claim back
	# the title loses `at 14:00` and nothing reports it. Every other row here is refused
	# earlier, so none of them exercises that path at all.
	("Dentist appointment Monday at 14:00", "at 14:00"),
	# A range names an end, and an end has nowhere to go (`#798`).
	("Meeting 14:00-15:00", "14:00"),
	# Not a time at all.
	("Broken at 25:00", "at 25:00"),
)


@pytest.mark.parametrize(("text", "reported"), LEAVES_A_TIME, ids=[one[0] for one in LEAVES_A_TIME])
def test_a_time_that_cannot_be_placed_stays_in_the_title_and_is_reported (
	text: str, reported: str
) -> None:
	"""Rule 1, on the field that was added last — and it failed here first.

	**`Dentist appointment Monday 14:00` lost `14:00` from its title and set nothing**, because
	claiming the span is what lets a bare day be seen as last, and that claim has to happen
	before anybody knows whether the time can be used. The claim is provisional now and is
	given back.

	**And the reporting is `#797`'s own recommendation** — *the cheapest honest half is the
	telling*. Silence is what cost two sightings before this was filed.
	"""

	captured = _parse(text)

	assert captured.title == text, "a time this grammar will not use must stay where it was"
	assert captured.snooze is None
	assert captured.due is None
	assert captured.starts_at is None
	assert reported in captured.unparsed


def test_a_written_date_takes_a_written_time_however_the_date_was_spelled () -> None:
	"""`SR#1239`. The same sentence written two ways gave two different answers.

	A weekday, ``today`` and ``tomorrow`` are resolved to a day before the clock is placed, so
	the time landed on them. **An ISO date stays a string**, so it was skipped — and the clock
	then fell all the way through to the fallback and invented a ``starts_at`` of *today*, on a
	line whose only date was a deadline.

	    by 2026-09-02 17:00      due 2 Sep at 17:00                    <- right
	    by 2026-09-02 at 17:00   due 2 Sep, and a start today at 17:00  <- the word *at*

	**Every preposition, because the fall-through was not particular about which field it
	robbed** — a deferred line lost its clock the same way.
	"""

	for line, field in (
		("Pay it by 2026-09-02 at 17:00", "due"),
		("Start it on 2026-09-02 at 17:00", "starts_at"),
		("Hide it from 2026-09-02 at 17:00", "snooze"),
	):
		captured = _parse(line)
		wanted = datetime.datetime(2026, 9, 2, 17, 0)

		assert getattr(captured, field) == wanted, (
			f"{line!r}: {field} is {getattr(captured, field)!r}"
		)

		invented = [
			name
			for name in ("due", "starts_at", "snooze")
			if name != field and getattr(captured, name) is not None
		]

		assert not invented, f"{line!r} also set {invented}, which nobody asked for"


def test_a_time_beside_a_date_that_already_has_one_is_reported_not_used () -> None:
	"""The half of `SR#1239` that keeps the old rule, and without it the fix over-reaches.

	``2026-09-02T17:00`` has said its own time. A second clock beside it is not a correction
	and not a range this grammar can hold, so it goes back into the title and is reported —
	rule 1 — rather than overwriting what the writer already wrote.
	"""

	captured = _parse("Pay it by 2026-09-02T17:00 at 18:00")

	assert captured.due == "2026-09-02T17:00", "the written instant was not left alone"
	assert captured.starts_at is None, "a second clock invented a start"
	assert "at 18:00" in captured.title, "a time this grammar will not use must stay where it was"


def test_a_time_with_no_day_at_all_still_means_today () -> None:
	"""`SR#797`'s behaviour, asserted because `SR#1239`'s fix runs directly past it.

	*Dentist at 3pm* names no day, so today is the only thing the clock can mean and inventing
	a start is right. What changed is that a line which **did** name a day never gets an
	invented one — so this is the case that says the narrowing stopped where it should.
	"""

	captured = _parse("Dentist at 3pm")

	assert captured.starts_at == datetime.datetime(2026, 7, 30, 15, 0)
	assert captured.due is None
	assert captured.title == "Dentist"


def test_a_one_to_one_is_not_one_minute_past_one () -> None:
	"""The case a looser pattern gets wrong, and it is how people write a recurring meeting.

	Two digits after the colon is what refuses it, so this is asserting the reason rather than
	the symptom — `1:1` and `3:5` are not times and never reach the hour check.
	"""

	for text in ("Weekly 1:1 with Bob", "Ratio is 3:5"):
		captured = _parse(text)

		assert captured.title == text
		assert captured.snooze is None
		assert captured.unparsed == ()


@pytest.mark.parametrize(
	("line", "title", "field", "expected"),
	[
		("Pay the rent by 1 september", "Pay the rent", "due", datetime.date(2026, 9, 1)),
		("Renew the domain due Sept 1", "Renew the domain", "due", datetime.date(2026, 9, 1)),
		# **`on` sets a start, exactly as `on friday` does.** A birthday is a thing that
		# happens rather than a deadline, and the calendar prefixes differ — the wrong one
		# writes "Due: Anna's birthday" into somebody's calendar.
		("Anna's birthday on 14 March", "Anna's birthday", "starts_at",
			datetime.date(2027, 3, 14)),
	],
)
def test_a_written_calendar_date_is_read_from_a_captured_line (
	line: str, title: str, field: str, expected: datetime.date
) -> None:
	"""`SR#1210`. Neither natural spelling for a date months away was read at all.

	`subroutine add "Pay the rent by 1 september"` left the whole phrase in the title and set
	nothing — with or without a repeat beside it, so this was the date grammar rather than an
	interaction with `SR#1208`.
	"""

	read = _parse(line)

	assert read.title == title, f"the phrase was left in the title: {read.title!r}"
	assert getattr(read, field) == expected, (
		f"{line!r} set {field}={getattr(read, field)!r}"
	)


def test_a_month_name_in_ordinary_prose_is_left_alone () -> None:
	"""The pattern most likely to eat something it should not — `SR#1210`.

	A month name is a word that appears in ordinary writing, unlike a sigil and unlike an ISO
	date. What keeps it out is that the date phrase is only ever reached through a preposition,
	and that a day number is required beside the month — so *"the September release"* is prose
	and *"by 1 September"* is a date.

	**§6.13 rule 1 is the standard being met**: a word may only vanish if a field was set, and
	the title may not contain a word the input did not. The generated-input invariants above
	enforce both across the whole grammar; this is the case they were least likely to reach,
	because their alphabet cannot produce a digit.
	"""

	for line in (
		"Ship the September release",
		"Ask about the March numbers",
		"Read the May report",
		"Book a table for August",
	):
		read = _parse(line)

		assert read.title == line, f"prose lost a word: {line!r} became {read.title!r}"
		assert read.due is None and read.starts_at is None, (
			f"{line!r} set a date nobody asked for"
		)

