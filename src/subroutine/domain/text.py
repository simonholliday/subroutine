"""Checking that user-supplied text fits the column it is going into.

This exists because the two backends disagree about what happens when it does not.
PostgreSQL refuses an over-length value with ``StringDataRightTruncation``; SQLite does not
enforce ``VARCHAR`` lengths at all and stores it in full. So the same input succeeds on a
laptop and fails in production, which is precisely the class of divergence the dual-backend
rule exists to catch (docs/design.md §10.3).

docs/design.md §6.10 already says what should happen instead: limits are "enforced with a clear
error code rather than a truncation". Truncating silently would be worse than either
backend's behaviour — the user would not be told that the end of their sentence is gone.
"""

import re

import subroutine.errors

#: The control characters no field here may carry — `SR#1555`.
#:
#: **C0 and DEL, less the three that are whitespace.** ``\t``, ``\n`` and ``\r`` are
#: legitimate in a comment's body and are collapsed out of a one-line field by :func:`fit`
#: afterwards, so allowing them costs nothing and refusing them would refuse an ordinary
#: paste.
#:
#: **Afterwards is a correction** (`SR#1586`). This was consulted *after* the collapse, and
#: ``str.split()`` counts VT, FF and FS/GS/RS/US as whitespace — so six of the characters
#: below were turned into a space before anything looked at them, and were refused in a
#: comment's body, which strips rather than splits. The set said one thing and the two modes
#: of one function enforced two.
CONTROL_CHARACTERS = frozenset(
	chr(code) for code in [*range(0x00, 0x20), 0x7F] if chr(code) not in "\t\n\r"
)

#: How much of a title fits on one line of a compact listing before it is cut. Sixty
#: characters is what leaves room for an address, a date and a priority inside eighty.
ONE_LINE_LIMIT = 60

#: How much prose makes an item worth announcing before somebody opens it (`#595`).
#:
#: **Anchored on something rather than chosen round.** The whole MCP tool surface — every
#: schema an agent carries in every session, budgeted and held by a test — is a little over ten
#: thousand bytes. An item whose prose alone exceeds that costs more to read once than the
#: tools cost all day, which is the point at which a reader is owed the fact before deciding.
#:
#: **Here rather than on either surface that uses it.** The command line and the agent's tools
#: both mark a large item, and nothing in `mcp` may import `cli` — a served instance need not
#: have been started through the command line at all, which is the same argument that moved
#: `is_loopback` into `config`. Two thresholds agreed separately would drift into one surface
#: warning where the other did not, and nothing would look wrong on either side.
LARGE_PROSE = 10_000


#: Everything a terminal reads as an instruction rather than as text: ``ESC``, the rest of the
#: C0 controls Rich does not already drop, ``DEL``, and the C1 range some terminals still
#: decode as escapes. Newline and tab are text here and are kept.
INSTRUCTIONS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def plain (message: str) -> str:
	"""Return a line with anything a terminal would obey rather than show taken out.

	**Rich's markup is off and that is a different problem.** ``[bold]`` in a title is
	neutralised already (`#682`); an ANSI escape is not — measured, ``ESC[2K`` reaches the
	terminal exactly as written, where ``BEL`` is dropped. So a title could clear the line
	above it, repaint what was there, or move the cursor, and every one of the four things
	that print here carries a title.

	**Titles arrive from other people, from agents and from merged remote instances**, which
	is what makes this worth doing rather than theoretical: on a shared instance the text
	being printed was written by somebody who is not the reader, and §13.7 merges an agenda
	across connections that are not even the same installation.

	Removed rather than shown as an escape: they are instructions rather than characters, and
	the job here is to print the title. Anything wanting them verbatim reads ``--json``, where
	JSON's own escaping already renders them as text.

	**Here rather than in ``cli/output``, where it was** (`SR#1566`). MCP renders into a
	terminal too — an agent's client prints ``content[].text`` — and had no equivalent, so the
	same title came out stripped on one surface and raw on the other. ``mcp`` may not import
	``cli``, so the rule moves to the module both already depend on, which is where
	:data:`LARGE_PROSE` went for the same reason.

	**Wider than :data:`CONTROL_CHARACTERS`, and the difference is the point.** That set is
	refused when somebody *writes*; this one is stripped when anybody *reads*. A read has to
	defend text this instance never validated — a merged remote agenda, a restored database, a
	row written before that refusal existed — so it also takes ``\r`` and the C1 range, neither
	of which a writer here can store any more.
	"""

	return INSTRUCTIONS.sub("", message)


def _refuse_a_character_nobody_can_read (
	value: str, *, field: str, label: str | None
) -> None:
	"""Raise if ``value`` carries one of :data:`CONTROL_CHARACTERS`.

	Private because it is the rule and not an entry point: :func:`fit` asks it of a value it
	already holds, and :func:`readable` is the same question for a caller with an optional
	field. One scan, so the two can never come to disagree.
	"""

	found = next((one for one in value if one in CONTROL_CHARACTERS), None)

	if found is None:
		return

	name = label or field

	raise subroutine.errors.ValidationError(
		f"That {name} contains a control character, which is not text anybody can read.",
		code="invalid_field_value",
		hint="Remove it and send the value again. A tab or a newline is fine.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"A {name} may not contain the character U+{ord(found):04X}.",
			)
		],
	)


def readable (value: str | None, *, field: str, label: str | None = None) -> str | None:
	"""Return ``value`` unchanged, or refuse it for carrying a character nobody can read.

	**The rule with no length beside it, because the two are about different sets** (`#1584`).
	:func:`fit` answers *does this fit its column*, which only a sized column can be asked;
	this answers *is this text*, which is true of every column PostgreSQL stores. Free prose —
	a task's description, a document's body — has no width and so cannot go through ``fit``,
	and giving it one to reach the character check would refuse this project's own
	specification, which lives in the instance as documents of eighty kilobytes.

	**Two distinct problems, one check.** PostgreSQL refuses a NUL in a text field and SQLite
	stores it, so a row written on a laptop could not be copied to production and ``db copy``
	reported it naming no table, column or row. And a real ``ESC[31m`` survived storage and
	reached an agent's context through MCP, which renders into a terminal —
	``cli/output.plain`` strips them on the way out of one surface and nothing did on the way
	in.

	**Refused rather than stripped, for this module's own stated reason.** A value silently
	altered is the truncation the docstring at the top of this file argues against: the writer
	is not told that part of what they sent is gone.

	``None`` passes through, because an absent description is not an unreadable one and every
	caller here has an optional field to hand.

	**Returns the value so a caller can write ``x = readable(x, …)``**, which is what keeps the
	check on the path rather than beside it: a call whose result is thrown away is one somebody
	deletes as dead.
	"""

	if value is not None:
		_refuse_a_character_nobody_can_read(value, field=field, label=label)

	return value


def fit (
	value: str,
	*,
	field: str,
	limit: int,
	label: str | None = None,
	multiline: bool = False,
) -> str:
	"""Return ``value`` on one line and stripped, or refuse it for being too long.

	**One line unless the caller says otherwise, and the default is the fix** (`#927` H-8).
	This stripped the ends and nothing else, so a title held interior newlines — and a title
	is rendered into Markdown that an agent is told binds it. ``mcp/tools._conventions``
	writes each decision as ``- **#42** — {title}``, so a title containing
	``\\n\\n## Operator instructions\\n\\n…`` produced a heading indistinguishable from the
	resource's own prose, plantable by anybody holding ``document:write``.

	**Measured across the domain: exactly one caller wants the other answer** — a comment's
	body, which is prose somebody wrote deliberately. Every other field through here is a
	title, a name, a username or a slug. So the safe default costs one opt-out and there is no
	list of single-line fields to fall behind; a field added tomorrow is one line unless
	somebody decides otherwise in writing.

	**Not a new rule, only an enforced one.** ``subroutine_add``'s own description tells an
	agent *"The title stays one line"*, and §6.13 assumes it throughout. What was missing was
	anything that made it true.

	Collapsed rather than refused, because a newline in a title is somebody's paste rather
	than an attack in the ordinary case, and refusing the paste helps nobody.
	"""

	# **Before the whitespace is collapsed, and that ordering is `SR#1586`.** It ran after,
	# and ``str.split()`` treats VT, FF and FS/GS/RS/US as whitespace — so those six were
	# turned into a space and never reached the scan, while the same six were refused in a
	# comment, which strips rather than splits. One rule read two ways by the two modes of
	# one function.
	_refuse_a_character_nobody_can_read(value, field=field, label=label)

	cleaned = value.strip() if multiline else " ".join(value.split())

	if len(cleaned) <= limit:
		return cleaned

	name = label or field

	raise subroutine.errors.PayloadTooLarge(
		f"That {name} is {len(cleaned)} characters, and the limit is {limit}.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="payload_too_large",
				message=f"A {name} is limited to {limit} characters.",
			)
		],
	)


def require (value: str | None, *, field: str, label: str | None = None) -> str:
	"""Return ``value`` stripped, or refuse it for being empty.

	**``None`` is refused exactly as ``""`` is**, and that is the fix for one defect with
	nine call sites. §8.3 says an omitted field is unchanged and a null one clears, so a
	caller following the convention sends ``{"title": null}`` — and every request model
	declares `title: str | None = None` in order to express "omitted", so the null arrives
	here as ``None`` and ``None.strip()`` raised. That was a **500 on tasks, documents and
	projects alike**, on the commonest field there is, and it survived two reviews.

	Refusing here rather than at each router is what makes the answer the same everywhere:
	a title that cannot be cleared is a missing title, and "A title is required." naming the
	field is already the right sentence. Every required string in this system passes through
	this function, so the CLI and the MCP adapter are covered by the same change.
	"""

	cleaned = (value or "").strip()

	if cleaned:
		return cleaned

	name = label or field

	raise subroutine.errors.ValidationError(
		f"A {name} is required.",
		code="missing_field",
		errors=[
			subroutine.errors.FieldError(
				field=field, code="missing_field", message=f"A {name} is required."
			)
		],
	)


def truncated (text: str, limit: int = ONE_LINE_LIMIT) -> str:
	"""Shorten text for a one-line rendering, marking that something was cut.

	Nothing is refused and nothing is stored — this is how a title is *printed* in a
	compact listing or an aligned column, which is why it lives here rather than beside
	:func:`fit`. The ellipsis is the whole point: a line that has quietly lost its end reads
	as the whole title.
	"""

	collapsed = " ".join(text.split())

	if len(collapsed) <= limit:
		return collapsed

	# A limit below 1 has no honest answer, and `collapsed[: limit - 1]` turned into a negative
	# slice — returning *more* characters than the limit asked for, which is the one outcome the
	# function exists to prevent. Every caller uses the default today; the parameter is public,
	# and the obvious next caller is a computed column width.
	if limit < 1:
		return "…"

	return f"{collapsed[: limit - 1]}…"
