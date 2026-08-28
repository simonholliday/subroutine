"""What `SR#1539` and `SR#1547` assert about the four surfaces, held rather than printed.

**Two decisions, one question.** `SR#1539`: a surface may lack a capability and may never leave
somebody stuck — *no dead ends rather than no differences*, because §21.2's tool budget, §1.4's
progressive disclosure and §12.4's recovery property each make "everything everywhere" the wrong
rule. `SR#1547`: whatever a surface *does* offer, it calls by the same word as the others — no
dead ends, and nobody learns this product twice.

**Nothing here has to be perfect for this to be worth having**, which is the whole reason it is
a ratchet. Simon's words on 2026-08-28: *"We might not be perfect on these criteria yet. But we
should be able to measure where we are."* So the assertions below say *this cannot get worse*,
not *this is finished*.

**And `scripts/parity.py` is imported rather than described.** A report nobody executes is a
paragraph, and `SR#146` is what a paragraph costs: it stated the same four numbers on
2026-08-01 and every one of them was wrong three weeks later, silently.
"""

import pathlib
import shutil
import sys
import typing

import pytest

import conftest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import parity

#: What `SR#1539`'s no-dead-ends clause currently costs, measured 2026-08-28.
#:
#: **A ceiling, never a target.** Twenty of the forty-six excuses that owe a signpost name no
#: way through, and the population is genuinely mixed: some are missing a signpost and some are
#: boundaries wearing the wrong label — ``create_workspace`` is an instance-tier permission no
#: role carries, and ``calendars`` is refused because a feed URL is an unauditable bearer
#: credential. Splitting `budget` and `disclosure` into those two meanings is a decision per
#: entry, deferred by `SR#1539` to after the code review.
#:
#: **Lower it when one is answered; never raise it.** Raising it is adding a surface somebody
#: can be stuck on, which is the one thing the decision forbids.
SILENT_CEILING = 20


def test_the_report_reads_every_surface_it_claims_to () -> None:
	"""A scan that reads nothing reports perfect parity, and this project has met that four times.

	**Floors before comparisons**, because every number below is a count of things found: a
	broken enumerator returns zero, zero is not more than zero, and a guard built on that would
	go green at exactly the moment it stopped looking. The four here are independent — the
	routers, the client protocol, the Typer app's package and the MCP catalogue's — so one
	failing is caught by its own floor rather than by the others agreeing.
	"""

	report = parity.measured()

	assert report.routes > 50, f"only {report.routes} routes were found, so nothing was read"
	assert report.protocol > 50, f"only {report.protocol} client methods were found"

	for edge in report.edges:
		assert edge.reaches > 0, f"{edge.name} was measured as reaching nothing at all"
		assert edge.total > 50, f"{edge.name} classified only {edge.total} capabilities"


def test_no_surface_has_a_capability_that_is_neither_reached_nor_excused () -> None:
	"""`SR#146`'s rule, stated where the report can see it.

	``tests/test_reach.py`` already holds this on all three of its edges and holds it per
	capability, which is the version that names the offender. This says the same thing about
	the **totals**, so a register that stopped being consulted — rather than an entry that went
	missing from one — is caught here.

	**Not a second copy of that guard.** It compares what was *enumerated* against what was
	*classified*; the file it reports on compares each subject against each register.
	"""

	report = parity.measured()

	assert report.edges[0].total == report.routes, (
		f"{report.routes} routes are mounted and {report.edges[0].total} are accounted for, so "
		f"some route is neither reached by a client nor excused in writing"
	)

	for edge in report.edges[1:]:
		assert edge.total == report.protocol, (
			f"{report.protocol} client methods exist and {edge.name} accounts for {edge.total}"
		)


def test_no_new_excuse_leaves_somebody_with_nowhere_to_go () -> None:
	"""`SR#1539`'s no-dead-ends clause, enforced in the only direction that is honest today.

	An excuse of a kind that owes a signpost — the capability exists, this surface simply does
	not foreground it — has to name where the reader goes instead. Twenty currently do not, and
	fixing those is deferred; **adding a twenty-first is not**.

	**The detector under-reports on purpose.** It looks for a citable destination — a command, a
	tool, the escape hatch, or a route — rather than for prose, because checking for wording
	would be a spelling test on somebody's sentence. So an excuse that signposts in words it
	cannot cite counts as silent, which makes this a ceiling on the work and never a claim that
	a gap has been closed.
	"""

	report = parity.measured()

	assert len(report.silent) <= SILENT_CEILING, (
		f"{len(report.silent)} excuses owe a signpost and give none, against a ceiling of "
		f"{SILENT_CEILING}. A capability nobody can reach from a surface, with nothing saying "
		f"where to go instead, is the second-class user SR#1539 exists to refuse:\n"
		+ "\n".join(f"  {register}[{subject}]" for register, subject in report.silent)
	)

	# **The floor beside the ceiling**, because a detector that started matching everything
	# would satisfy the line above perfectly and report that every excuse signposts.
	assert report.silent, (
		"every excuse now names a way through, which is either the work being finished — in "
		"which case lower SILENT_CEILING to nought and delete this — or the detector matching "
		"anything at all"
	)


def test_the_browser_is_measured_even_though_it_is_not_yet_enforced () -> None:
	"""The surface `SR#1539` names as the gap, counted so that it cannot be forgotten.

	`tests/test_reach.py` guards three edges and the browser is not one of them — it appears
	there only as an *excuse* for the routes that serve it. So the surface built for the
	audience `SR#1382` is expanding to is the one the equality guard cannot see.

	**Reported and deliberately not asserted.** Making it a fourth edge means classifying every
	route it does not reach with a written reason each, which is judgement rather than typing
	and is `SR#1539`'s after-the-review work. What this holds is that the number is *known* —
	the state that lets somebody decide, rather than rediscover.

	**Executed, not scanned**, so it needs Node — required in CI for the reason
	``tests/test_web.py`` states, since a surface silently unmeasured is what this whole file
	exists to prevent.
	"""

	if shutil.which("node") is None:
		if conftest.required("SUBROUTINE_TEST_REQUIRE_NODE"):
			pytest.fail(
				"no JavaScript runtime on PATH, so the browser's reach cannot be executed.\n\n"
				"SUBROUTINE_TEST_REQUIRE_NODE is set, so a missing runtime fails the run rather "
				"than leaving the one surface SR#1539 names as the gap unmeasured."
			)

		pytest.skip("no JavaScript runtime on PATH, so the browser's reach cannot be executed")

	report = parity.measured()

	assert report.browser is not None, (
		f"the browser's builders could not be executed: {report.browser_absent}"
	)

	assert report.browser > 10, (
		f"the browser was measured as building {report.browser} route shapes, which is fewer "
		f"than it has controls — the enumeration read almost nothing"
	)

	assert report.browser < report.routes, (
		"the browser reaches at least as many route shapes as the API has routes, which would "
		"mean the shaping has stopped collapsing identifiers and every row counts as a route"
	)


def test_the_report_names_every_surface_it_measured () -> None:
	"""The rendering, because a report that prints nothing passes every check above.

	`SR#1540`'s whole argument is that somebody runs this and reads it. The counts can be
	perfect while :func:`parity.render` drops a row, and the numbers are gathered by a function
	no reader ever calls.
	"""

	rendered = parity.render(parity.measured())

	for name in ("client protocol", "terminal", "agent tools", "browser"):
		assert name in rendered, f"the report does not mention {name}"

	assert "SR#1539" in rendered, "the report does not say which decision it is measuring"


def test_every_agent_tool_is_named_as_a_command_or_excused () -> None:
	"""`SR#1547`: an agent dropping to a shell types the word it already knows.

	Thirteen of fifteen already match exactly — `add`, `update`, `done`, `claim`, `link`,
	`list`, `search`, `show`, `comment`, `project`, `changes`, `journal`, `whoami`. That is the
	state worth protecting, and it is `tests/test_reach.py`'s rule applied to **words** rather
	than to capabilities: a name reaches both surfaces unless somebody wrote down why not.

	**It found `document` on its first run.** The terminal calls that group `doc`, so
	`subroutine document` is refused with *Did you mean 'comment'?* — a suggestion pointing at
	a different kind of record, which is `SR#1547`'s *"now means something different"* arriving
	as help. `SR#1549`.
	"""

	report = parity.measured()
	named = set(report.matched_tools) | set(report.unmatched_tools)

	assert len(named) > 10, f"only {len(named)} tools were read, so this is checking nothing"

	unexplained = [one for one in report.unmatched_tools if one not in parity.NOT_A_COMMAND]

	assert not unexplained, (
		f"these agent tools have no terminal command of the same name and no written reason: "
		f"{unexplained}. A word that exists on one surface and not the other is somebody "
		f"learning this product twice."
	)


def test_no_tool_is_both_named_as_a_command_and_excused_from_being_one () -> None:
	"""An excuse that has quietly become true reads as a considered decision and is not.

	`tests/test_reach.py` learned this the expensive way — three entries stayed after the gap
	they described was closed, still naming an item, still reading as deliberate. Every
	register in this repository owes the same question: **what makes the entry go away?**
	"""

	report = parity.measured()
	matched = set(report.matched_tools)
	stale = sorted(name for name in parity.NOT_A_COMMAND if name in matched)

	assert not stale, (
		f"{stale} are excused from having a terminal command and now have one — delete the "
		f"entries, which is what closes whatever they name"
	)

	gone = sorted(
		name for name in parity.NOT_A_COMMAND
		if name not in matched and name not in report.unmatched_tools
	)

	assert not gone, f"{gone} are excused and are not tools any more"


def test_no_user_facing_string_spells_a_term_the_other_way () -> None:
	"""`SR#1547`, and the register is deliberately small.

	**Docstrings are excluded and that is the whole trick.** A comment explaining the parent
	rule may say ``subtask``; a refusal may not. What a developer reads about the product and
	what somebody using it reads are different corpora, and only the second is scanned.

	**A word that is also a published identifier cannot be in the register.** ``todo`` returns
	eight hits of which seven are the status category *key* — which callers send — so flagging
	them would be guarding a spelling instead of a thing and correcting them would break the
	contract. Growing the register is a judgement per term, deferred by `SR#1547`.
	"""

	report = parity.measured()

	# **The floor first.** Every count below is of things found, so a scan that read nothing
	# would report perfect consistency — which is this project's most-repeated failure.
	assert report.spoken > 5_000, (
		f"only {report.spoken} user-facing strings were read, so the scan is blind"
	)

	assert not report.misspellings, (
		"a user meets two spellings of one word:\n"
		+ "\n".join(
			f"  {where}  says {variant!r} where this product says {word!r}"
			for where, variant, word in report.misspellings
		)
	)


def test_the_spelling_scan_can_see_an_offender_through_its_own_entry_point (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#405`: a guard is tested by feeding it a defect the way the real code arrives.

	Both halves, and they fail differently. **The variant in a string** has to be found — that
	is the check. **The same word in a docstring** must not be, because a scan that cannot tell
	the two apart is one that either misses refusals or forbids developers their own prose, and
	the register would then be argued down rather than grown.
	"""

	assert parity.TERMS, "the register is empty, so there is nothing this could find"

	variant = sorted(parity.TERMS)[0]
	word, _reason = parity.TERMS[variant]

	(tmp_path / "offender.py").write_text(
		f'"""A docstring saying {variant} freely, which is allowed."""\n'
		f'MESSAGE = "A {variant} belongs to the same project as its parent."\n',
		encoding="utf-8",
	)

	found, read = parity.misspelled(tmp_path)

	assert read == 1, f"the scan read {read} strings where the docstring is not one of them"
	assert [(one[1], one[2]) for one in found] == [(variant, word)], (
		f"the scan did not find {variant!r} in a refusal, so it is not checking anything"
	)

	(tmp_path / "offender.py").write_text(
		f'"""Only a docstring saying {variant}, which a developer may write."""\n',
		encoding="utf-8",
	)

	clean, _read = parity.misspelled(tmp_path)

	assert not clean, (
		f"the scan flagged {variant!r} in a docstring — developers may write about the thing "
		f"in their own prose, and a register that forbids it will be argued down instead of "
		f"grown"
	)


def test_a_built_path_collapses_the_values_it_was_built_from () -> None:
	"""`SR#1550`, and the mutation that passed is why this exists.

	`_shape` turns one built request into the route it names, so five rows of `/tasks/{x}` count
	once. It recognises a segment by *being one of the values the builders were handed* — and
	the first version listed those values a second time by hand, twenty lines from where they
	are constructed. Two copies that agree until one moves, in the script written to find that.

	**Blinding the derivation did not fail anything.** `test_the_browser_is_measured_…` bounds
	the count between ten and the number of routes, and a shaping that stops collapsing pushes
	31 to somewhere still inside that range. So the report would have been quietly wrong — the
	browser reading as reaching more than it does — which is the one outcome `SR#1540` exists to
	prevent.

	**The digit and the word are asserted apart**, because `isdigit()` alone covers most
	placeholders and would make the derived set look load-bearing when it was not: the third
	assertion is the one that fails when `_stood_in` returns nothing.
	"""

	assert parity._shape("/tasks/1/comments", set()) == "/tasks/{x}/comments"

	assert parity._shape("/workspaces/w/members", {"w"}) == "/workspaces/{x}/members"

	assert parity._shape("/workspaces/w/members", set()) == "/workspaces/w/members", (
		"a non-numeric placeholder collapsed without being recognised, so the derived set is "
		"not what decides it and this test cannot see it go blind"
	)

	# **Nothing else moves.** A shaping that collapsed real path words would report every
	# surface reaching one route, which passes a bound written the other way up.
	assert parity._shape("/tasks", {"tasks"}) == "/{x}", "the set is consulted for every segment"
	assert parity._shape("/agenda", set()) == "/agenda"


class _Placeholders(typing.NamedTuple):
	"""A stand-in for the instance the builders are handed, so the derivation can be driven."""

	application: str
	slug: str
	task: int
	blank: str


def test_the_values_a_path_is_shaped_against_come_from_the_instance_that_built_it () -> None:
	"""`SR#1550`. The half the test above cannot see, and it is the half that drifted.

	`_shape` is handed a set, so driving it directly says nothing about **where that set comes
	from** — blinding the derivation left every assertion up there green, which is how the
	hand-written copy went unnoticed in the first place.

	Driven against a synthetic instance rather than the real one, which is `SR#405`'s rule and
	also keeps this test off `fastapi`: what is being checked is the rule for turning an object's
	fields into path segments, and any ``NamedTuple`` exercises it.

	Three properties, and each has cost something somewhere in this repository: the values are
	**found**, the application is **not** one (it is an object, never a path segment), and an
	**empty** value is not an identifier — an empty segment would otherwise collapse every double
	slash and every trailing one into `{x}`.
	"""

	derived = parity._stood_in(_Placeholders(application="app", slug="w", task=1, blank=""))

	assert "w" in derived, "the derivation found no values, so nothing shapes a path"
	assert "1" in derived, "a numeric value was dropped before it could be stringified"
	assert "app" not in derived, "the application is not a value a path could carry"
	assert "" not in derived, "an empty value would collapse every empty segment"
