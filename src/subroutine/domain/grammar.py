"""A search line written the way a person types it — item `#1806`, design `#1801` §6.

``is:bug urgent>3 deploy script`` was `#1216`'s example and this is the settled spelling of it:
``type:bug urgency>3 deploy script``, which reads as ``type.eq=bug``, ``urgency.gt=3`` and a
search for *deploy script*.

**Sugar over the registry and nothing else.** Every term a line may carry is a
:class:`subroutine.domain.filtering.Property` that declares itself filterable, so the language
cannot outrun what the server answers — `#1801` §4's rule, and the reason Simon refused
*build the missing filter and defer the grammar* on 2026-08-25. A field added to the registry
tomorrow is typeable tomorrow, and nothing here lists a name.

**The server parses, so there is one parser** (`#1801` §6). Three clients would otherwise grow
three implementations of one language and disagree about the interesting half — which is this
codebase's signature defect, on the vocabulary that decides what a caller may ask.

**A term this cannot read stays in the text and is reported.** `#615`'s defect is a plausible,
complete, wrong answer: ``every last thursday`` is the worked precedent, where a phrase that
was not understood silently vanished from a title and left a date set from the wreckage. So
``15:30`` is not a filter on a field called ``15`` and is not an error either — it stays part
of what is searched for, which is what somebody typing a time meant.

**That rule is also what makes this safe to put on ``q``**, the parameter a search box has
always sent. Only a term naming a field the registry really carries is taken out of the text;
everything else is searched for verbatim, so a query that meant nothing to this grammar
behaves exactly as it did before. The one case that changes is a search for the literal words
``status:open``, and :attr:`Read.unread` is where the caller is told.
"""

import typing

import subroutine.domain.filtering

#: What separates a field from its value in the written form. Not :data:`filtering.SEPARATOR`,
#: which separates a field from its *operator* in the dotted form — two punctuation marks doing
#: two jobs, and the line grammar has no way to spell an operator.
NAMES = ":"

#: The comparison operators that have a written symbol, longest first.
#:
#: **Longest first is load-bearing**, because ``>=`` starts with ``>``: matched in dictionary
#: order, ``urgency>=3`` would read as ``urgency`` greater than ``=3``, which is a value no
#: kind can read and would be reported as an unreadable term rather than working.
#:
#: **``:`` is deliberately absent and is handled apart.** It means *equals* on most fields and
#: *is* on a condition, and which one it means depends on the value — so it is the one symbol
#: whose operator cannot be decided by the symbol.
SYMBOLS: tuple[tuple[str, str], ...] = (
	(">=", "gte"),
	("<=", "lte"),
	("!=", "ne"),
	(">", "gt"),
	("<", "lt"),
)

#: What separates the several values of an ``in`` — the dotted form's own separator, so
#: ``tag:ops,web`` and ``tag.in=ops,web`` are one question written two ways.
BETWEEN_VALUES = subroutine.domain.filtering.IN_SEPARATOR

#: What quoting a value does: makes it a value.
#:
#: **The escape the reserved words need** (`#1806`'s owed collision rule). ``assignee:unset``
#: asks whether anybody has it, because :data:`filtering.CONDITIONS` are reserved in the value
#: position and a grammar that resolved them against usernames would answer a different
#: question on an instance that happens to have an account called *unset*. Quoting says the
#: opposite: ``assignee:"unset"`` is the person of that name.
#:
#: **Reserved words win unquoted, rather than the account winning**, because the condition is
#: the far commoner question and because the quoted form is always available where the account
#: is meant. The reverse would leave *has anybody got this* unaskable on one instance in a
#: thousand, silently, with no spelling to fall back to.
QUOTES = "\"'"


class Read (typing.NamedTuple):
	"""What one written line turned out to be asking."""

	#: Dotted parameters, in the shape :func:`filtering.understood` takes — so a line compiles
	#: to exactly what somebody could have typed by hand, and nothing here needs to know how a
	#: comparison is turned into SQL.
	parameters: list[tuple[str, str]]

	#: What is left after the terms are taken out: the words to search for, or ``None`` when
	#: there are none. ``None`` rather than ``""`` because a listing distinguishes *no search*
	#: from *a search for nothing*, and `#880` records what the second one cost.
	words: str | None

	#: Terms that named a real field and could not be read, each with why.
	#:
	#: **A term naming no field at all is not here**, because it is not a term — it is words,
	#: and it is in :attr:`words` where somebody typing ``15:30`` meant it to be. This holds
	#: the ones where the caller plainly meant a filter and it did not work: a field that takes
	#: no such operator, or a value the field cannot read.
	unread: list[str]


def read (line: str | None, *, entity: str) -> Read:
	"""Parse a written search line into dotted parameters and the words left over.

	**Whitespace separates terms, and a quoted value may hold spaces**: ``title:"deploy
	script"`` is one term. Everything that is not a term is a word.

	**Nothing here is refused.** A line is what somebody typed, so the failure this is built to
	avoid is silence rather than strictness — a term that cannot be read is searched for and
	reported, and the caller can see both what was applied and what was not.
	"""

	if line is None:
		return Read(parameters=[], words=None, unread=[])

	available = subroutine.domain.filtering.filters(entity)
	parameters = []
	words = []
	unread = []

	for token in _tokens(line):
		term = _as_a_term(token, available)

		if term is None:
			words.append(token)
			continue

		name, value, complaint = term

		if complaint is not None:
			words.append(token)
			unread.append(complaint)
			continue

		parameters.append((name, value))

	return Read(
		parameters=parameters,
		words=" ".join(words) if words else None,
		unread=unread,
	)


def _tokens (line: str) -> list[str]:
	"""Split a line into terms and words, keeping a quoted value whole.

	**A quote only groups after a separator**, which is what keeps an apostrophe out of it:
	``don't`` is one word and not the start of a quoted string that never ends. So the quote
	characters are ordinary text everywhere except immediately after ``:`` or a symbol.
	"""

	found = []
	current = ""
	closing = ""

	for character in line:
		if closing:
			current += character

			if character == closing:
				closing = ""

			continue

		if character in QUOTES and current and current[-1] in _OPENS_A_VALUE:
			closing = character
			current += character

			continue

		if character.isspace():
			if current:
				found.append(current)

			current = ""

			continue

		current += character

	if current:
		found.append(current)

	return found


#: The characters after which a quote opens a value rather than being one. Derived from the
#: symbols, so adding an operator cannot leave its quoted form unsupported.
_OPENS_A_VALUE = frozenset(NAMES + "".join(symbol for symbol, _operator in SYMBOLS))


def _as_a_term (
	token: str, available: dict[str, subroutine.domain.filtering.Filterable]
) -> tuple[str, str, str | None] | None:
	"""Read one token as a dotted parameter, or report that it is not a term at all.

	Returns ``None`` for something that is words — a token with no separator in it, or one
	whose left-hand side names no field this entity has. Both are ordinary: ``deploy`` is a
	word and so is ``15:30``, and neither is a mistake worth mentioning.

	Returns a complaint alongside where the token plainly *is* a term and cannot be honoured,
	which is the case worth telling somebody about.
	"""

	for symbol, operator in SYMBOLS:
		field, found, value = token.partition(symbol)

		if found and field in available:
			return _resolved(field, operator, _unquoted(value), available[field], token)

	field, found, value = token.partition(NAMES)

	if not found or field not in available:
		return None

	value, was_quoted = _unquoted(value), _quoted(value)
	operator = _colon_means(value, available[field], quoted=was_quoted)

	return _resolved(field, operator, value, available[field], token)


def _colon_means (
	value: str,
	filterable: subroutine.domain.filtering.Filterable,
	*,
	quoted: bool,
) -> str:
	"""Say which operator ``:`` stands for on this field, given what was written after it.

	Three readings, and the value decides between them because the symbol cannot:

	* one of the reserved condition words, unquoted, on a field that takes ``is`` — the
	  condition, which is what ``assignee:unset`` is asking;
	* several values separated by a comma, on a field that takes ``in`` — any of them;
	* anything else — equality.
	"""

	if (
		not quoted
		and value in subroutine.domain.filtering.CONDITIONS
		and subroutine.domain.filtering.IS in filterable.operators
	):
		return subroutine.domain.filtering.IS

	if BETWEEN_VALUES in value and subroutine.domain.filtering.IN in filterable.operators:
		return subroutine.domain.filtering.IN

	return "eq"


def _resolved (
	field: str,
	operator: str,
	value: str,
	filterable: subroutine.domain.filtering.Filterable,
	token: str,
) -> tuple[str, str, str | None]:
	"""Return the dotted parameter this term makes, or say why the field will not take it.

	**The operators come from the registry rather than from this module**, so a field that
	refuses equality refuses it here for the reason it refuses it there — ``created_at:today``
	is `#815`'s deliberate absence, since two instants are equal to the microsecond and almost
	never to the caller, and the complaint says which operators it does take instead.
	"""

	name = f"{field}{subroutine.domain.filtering.SEPARATOR}{operator}"

	if operator not in filterable.operators:
		return (
			name,
			value,
			f"{token!r} was searched for as text: {field!r} does not compare that way. "
			f"It takes {', '.join(sorted(filterable.operators))}.",
		)

	if not value:
		return (name, value, f"{token!r} was searched for as text: it names no value.")

	return (name, value, None)


def _unquoted (value: str) -> str:
	"""Return a value with its surrounding quotes taken off, if it had a matching pair."""

	if len(value) >= 2 and value[0] in QUOTES and value[-1] == value[0]:
		return value[1:-1]

	return value


def _quoted (value: str) -> bool:
	"""Report whether this value arrived wrapped in a matching pair of quotes."""

	return len(value) >= 2 and value[0] in QUOTES and value[-1] == value[0]
