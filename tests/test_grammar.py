"""A written search line, parsed — item `SR#1806`, design `SR#1801` §6.

`tests/test_api_filtering.py` drives the wire; this drives the language. Every case here is
about what a *person types*, so the failure being guarded against is silence: a term that was
not understood and vanished, which is `SR#615`'s defect and the reason the line is reported
back rather than merely applied.
"""

import pytest

import subroutine.domain.filtering
import subroutine.domain.grammar


def test_the_worked_example_parses () -> None:
	"""`SR#1216`'s own example, in the spelling `SR#1801` §5 settled.

	The spike proposed ``is:bug urgent>3 deploy script``. ``is`` became a *condition* with
	exactly two reserved words, so a grammar also spelling a value match ``is:`` would make one
	word mean two things across two surfaces — decision `SR#1267` §2's naming hazard. The
	general form is ``field:value``, so this is what it looks like now.
	"""

	read = subroutine.domain.grammar.read(
		"type:bug urgency>3 deploy script", entity="task"
	)

	assert read.parameters == [("type.eq", "bug"), ("urgency.gt", "3")]
	assert read.words == "deploy script"
	assert read.unread == []


def test_a_term_naming_no_field_is_searched_for_rather_than_refused () -> None:
	"""**The property that makes this safe to put on ``q``** — `SR#1806`.

	``q`` is what a search box has always sent, and reinterpreting a shipped parameter is how a
	caller comes to believe it asked something it did not. Only a term naming a field the
	registry really carries is taken out of the text, so everything else is searched for
	verbatim and a query that means nothing to this grammar behaves exactly as before.

	``15:30`` is the case that decides it: somebody typing a time has not asked about a field
	called ``15``, and neither refusing nor dropping it would be an answer they wanted.
	"""

	read = subroutine.domain.grammar.read("15:30 meeting notes", entity="task")

	assert read.parameters == []
	assert read.words == "15:30 meeting notes"
	assert read.unread == [], "a token that is words is not a term that failed"


def test_a_field_that_cannot_compare_that_way_is_searched_for_and_said_so () -> None:
	"""`SR#615`'s rule, and the operators come from the registry rather than from the parser.

	``created_at:today`` is `SR#815`'s deliberate absence — two instants are equal to the
	microsecond and almost never to the caller — so equality is not among that field's
	operators and this must not invent it. The term stays in the text, which is the half that
	stops it vanishing, *and* is reported, which is the half that stops it being silent.
	"""

	read = subroutine.domain.grammar.read("created_at:today", entity="task")

	assert read.parameters == []
	assert read.words == "created_at:today"
	assert len(read.unread) == 1
	assert "created_at" in read.unread[0]

	# **The refusal names what the field does take**, so somebody can correct it in one go.
	for operator in ("gt", "gte", "lt", "lte"):
		assert operator in read.unread[0]


def test_the_reserved_words_win_unquoted_and_quoting_is_the_escape () -> None:
	"""**The collision rule `SR#1806` owed** — what ``assignee:unset`` means where somebody is
	called *unset*.

	The condition wins, because *has anybody got this* is the far commoner question and because
	the account is still reachable: quoting a value says it is a value. The reverse would leave
	the common question unaskable on one instance in a thousand, silently, with nothing to fall
	back to.
	"""

	condition = subroutine.domain.grammar.read("assignee:unset", entity="task")

	assert condition.parameters == [("assignee.is", "unset")]

	person = subroutine.domain.grammar.read('assignee:"unset"', entity="task")

	assert person.parameters == [("assignee.eq", "unset")]


def test_a_comma_asks_for_any_of_them_where_the_field_takes_it () -> None:
	"""``tag:ops,web`` and ``tag.in=ops,web`` are one question written two ways.

	**A comma became illegal in a tag name for exactly this** (`SR#1804`, in
	``tags.refuse_a_reference``), so there is no reading of the separator that is ambiguous.
	"""

	read = subroutine.domain.grammar.read("status:open,done", entity="task")

	assert read.parameters == [
		(f"status{subroutine.domain.filtering.SEPARATOR}{subroutine.domain.filtering.IN}",
		 "open,done")
	]


def test_a_quoted_value_may_hold_spaces_and_an_apostrophe_may_not_open_one () -> None:
	"""Quoting groups a value; it does not turn every apostrophe into a quotation.

	``don't`` is one ordinary word. A quote only opens a value directly after a separator,
	which is what keeps a contraction from swallowing the rest of the line — a parser that read
	it as an unterminated string would take every word after it into one term.
	"""

	assert subroutine.domain.grammar.read("don't panic", entity="task").words == "don't panic"

	spaced = subroutine.domain.grammar.read('tag:"two words" rest', entity="task")

	assert spaced.parameters == [("tag.eq", "two words")]
	assert spaced.words == "rest"


def test_a_longer_symbol_is_matched_before_the_one_it_starts_with () -> None:
	"""``>=`` starts with ``>``, so dictionary order would read ``urgency>=3`` as ``>`` and
	``=3`` — a value no kind can read, reported as a broken term rather than working."""

	assert subroutine.domain.grammar.read("urgency>=3", entity="task").parameters == [
		("urgency.gte", "3")
	]
	assert subroutine.domain.grammar.read("urgency<=3", entity="task").parameters == [
		("urgency.lte", "3")
	]
	assert subroutine.domain.grammar.read("urgency!=3", entity="task").parameters == [
		("urgency.ne", "3")
	]


def test_nothing_here_names_a_field_and_the_entity_decides_which_exist () -> None:
	"""**The rule Simon held out for on 2026-08-25** — `SR#1801` §4.

	The language is sugar over the registry, so what is typeable is what is declared. This is
	the guard on that: a field filterable on one entity and not the other must be a term on the
	first and words on the second, with no list anywhere saying so.
	"""

	# `estimate_minutes` is a task's and not a document's.
	assert "estimate_minutes" in subroutine.domain.filtering.filters("task")
	assert "estimate_minutes" not in subroutine.domain.filtering.filters("document")

	assert subroutine.domain.grammar.read(
		"estimate_minutes<2h", entity="task"
	).parameters == [("estimate_minutes.lt", "2h")]

	elsewhere = subroutine.domain.grammar.read("estimate_minutes<2h", entity="document")

	assert elsewhere.parameters == []
	assert elsewhere.words == "estimate_minutes<2h"


@pytest.mark.parametrize("entity", sorted(subroutine.domain.filtering.PROPERTIES))
def test_every_filterable_field_can_be_written_as_a_term (entity: str) -> None:
	"""**Derived from the registry, so a field declared tomorrow is covered tomorrow.**

	The claim this file makes is that the grammar cannot outrun the registry. A parametrisation
	over a hand-written list could not check that; this walks what the registry declares and
	asserts every filterable field has *some* written form, with the operator taken from the
	field's own set rather than assumed.
	"""

	available = subroutine.domain.filtering.filters(entity)

	# **An assertion rather than a skip**, because a skip is what a broken walk looks like: if
	# `filters` ever answered with nothing, every parametrisation would skip and the suite
	# would be green about a grammar that could parse no term at all.
	assert available, f"{entity} declares no filters, so this case checked nothing"

	for field, filterable in sorted(available.items()):
		# The symbol for whichever operator this field really takes, preferring `:` since it
		# covers `eq` and `is` — the two most fields have.
		written = (
			f"{field}:x"
			if "eq" in filterable.operators
			else next(
				(
					f"{field}{symbol}x"
					for symbol, operator in subroutine.domain.grammar.SYMBOLS
					if operator in filterable.operators
				),
				f"{field}:{subroutine.domain.filtering.UNSET}",
			)
		)

		read = subroutine.domain.grammar.read(written, entity=entity)

		assert read.parameters, (
			f"{field!r} is filterable on {entity} and {written!r} produced no term — it takes "
			f"{sorted(filterable.operators)}"
		)
		assert read.unread == [], read.unread
