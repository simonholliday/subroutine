"""What the published package claims about itself has to be something that was checked.

``pyproject.toml``'s classifiers are rendered on the PyPI page, which is the first thing a
stranger reads about this project and one of the few surfaces nobody here ever looks at again.
A classifier is cheap to write and impossible to notice going stale, which is the combination
this repository keeps paying for.

The one that mattered is ``Operating System`` (`#245`): it said ``OS Independent`` while every
job in both workflows ran ``ubuntu-latest`` — ten of them, no exceptions — so the page
advertised a portability nothing had ever demonstrated. Not a lie anybody would be misled into
an install by, and exactly the shape of every other defect found the same week: **a claim
nothing checks.**
"""

import pathlib
import re
import tomllib

import pytest
import yaml

import subroutine.cli.main

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOWS = ROOT / ".github" / "workflows"

#: What a GitHub runner label demonstrates, in the vocabulary PyPI publishes.
#:
#: **Deliberately coarse.** ``ubuntu-latest`` proves Linux rather than Ubuntu, because that is
#: the claim a reader takes from it — nobody reads ``POSIX :: Linux`` as a statement about a
#: distribution. A runner label absent here fails rather than being ignored, so a matrix that
#: grows a platform this map has never heard of is a decision somebody has to make.
DEMONSTRATED_BY = {
	"ubuntu": "Operating System :: POSIX :: Linux",
	"macos": "Operating System :: MacOS",
	"windows": "Operating System :: Microsoft :: Windows",
}


def _runners () -> set[str]:
	"""Return every platform anything in CI has actually been run on.

	Reads ``runs-on`` off the workflows themselves rather than a list kept beside them, for the
	reason `#405` gives about every allow-list here: a list of what CI does is a second copy of
	what CI does, and the copy is the one that goes stale. A ``runs-on`` naming a matrix is
	followed into ``strategy.matrix.os``, because that is how a second platform would arrive.
	"""

	found: set[str] = set()

	for path in sorted(WORKFLOWS.glob("*.yml")):
		loaded = yaml.safe_load(path.read_text(encoding="utf-8"))

		for job in (loaded.get("jobs") or {}).values():
			runs_on = job.get("runs-on")

			if isinstance(runs_on, str) and "matrix." not in runs_on:
				found.add(runs_on)

				continue

			# `runs-on: ${{ matrix.os }}` — the platform is in the strategy, not here.
			found.update((job.get("strategy") or {}).get("matrix", {}).get("os") or [])

	return found


def _classifiers () -> list[str]:
	"""Return what the package tells an index about itself."""

	loaded = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

	return list(loaded["project"]["classifiers"])


def test_the_operating_systems_claimed_are_the_ones_something_has_run_on () -> None:
	"""`#245`. The classifier and the tested reality must agree, in both directions.

	**Widening the claim is done by running the suite somewhere, never by editing the list.**
	That is the whole point of deriving one side from the workflows: adding ``macos-latest`` to
	the matrix makes this fail until the classifier catches up, and removing the last runner of
	a platform makes it fail until the claim comes back down.
	"""

	runners = _runners()

	assert runners, "no `runs-on` was found at all — has this stopped reading the workflows?"

	unknown = sorted(
		runner
		for runner in runners
		if not any(runner.startswith(prefix) for prefix in DEMONSTRATED_BY)
	)

	assert not unknown, (
		f"CI runs on {unknown}, which DEMONSTRATED_BY has never heard of. Say what platform "
		f"that proves before the package claims anything about it."
	)

	demonstrated = {
		claim
		for runner in runners
		for prefix, claim in DEMONSTRATED_BY.items()
		if runner.startswith(prefix)
	}
	claimed = {line for line in _classifiers() if line.startswith("Operating System")}

	assert claimed == demonstrated, (
		f"the package claims {sorted(claimed)} and CI has only ever run on "
		f"{sorted(demonstrated)}. Add the platform to the matrix, or stop claiming it."
	)


def test_os_independent_is_never_the_claim () -> None:
	"""The specific string `#245` was filed about, kept as its own check.

	``Operating System :: OS Independent`` cannot be *demonstrated* — there is no runner for
	"every operating system" — so the comparison above can never produce it and would report it
	as an unsupported claim. That is the right answer for the wrong reason, and it would read
	as a matrix problem. This says the thing itself: it is a promise nothing can check.
	"""

	assert "Operating System :: OS Independent" not in _classifiers(), (
		"nothing can run on every operating system, so nothing can ever have demonstrated "
		"this — name the platforms that were actually tested"
	)


# ---- one licence, named once, and every statement of it derived (`SR#666`) ------------------


#: Every file that tells a reader which licence Subroutine is published under.
#:
#: **Not a list of files that mention licensing.** Plenty do and should: `scripts/check_licences`
#: names the copyleft licences it refuses in a *dependency*, `web/vendored` names the licences of
#: files we did not write, and several places now say what the AGPL used to require and no longer
#: does. Those are correct and none of them is a claim about what this package is.
#:
#: What is here is the claim itself, in each place somebody could read it and act on it.
#:
#: **`LICENSE` is deliberately absent**: it states its own identifier under an `Abbreviation`
#: heading rather than in a sentence, so the check below cannot see it — and
#: :func:`test_the_licence_file_is_the_licence_the_package_claims` reads that heading properly.
#: Listing it here as well produced a test that failed on a correct tree, which made every
#: falsification of the *others* look successful while they were failing for its reason.
STATES_THE_LICENCE = {
	"README.md": "the first and often only place anybody reads it",
	"CLA.md": "names it as the reason the agreement is needed at all",
	"CONTRIBUTING.md": "the same reason, told to somebody about to write code",
	"docs/hosting.md": "what an operator standing up an instance is agreeing to",
}


def _declared () -> str:
	"""Return the licence the package publishes to an index."""

	loaded = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

	return str(loaded["project"]["license"])


def test_the_licence_file_is_the_licence_the_package_claims () -> None:
	"""**The metadata and the file can disagree in total silence, and PyPI shows the metadata.**

	`pyproject.toml`'s `license` field is what an index renders and what a company's tooling
	reads; `LICENSE` is what a person opens. Changing one and not the other publishes a claim
	nobody in the repository is making — and nothing else here would notice, because both files
	are individually well-formed and neither is imported by anything.

	The abbreviation is read out of the licence rather than matched loosely, so this is the file
	*being* that licence rather than mentioning it somewhere. A licence with no `Abbreviation`
	section would fail here and should: swapping licence families is a thing somebody ought to
	be made to look at.
	"""

	declared = _declared()
	text = (ROOT / "LICENSE").read_text(encoding="utf-8")

	assert declared, "the package declares no licence at all"

	stated = re.search(r"^##\s+Abbreviation\s*\n+(\S+)\s*$", text, re.MULTILINE)

	assert stated is not None, (
		"LICENSE has no 'Abbreviation' section, so nothing here can check that the file is the "
		"licence the package claims — look at this test rather than deleting it"
	)
	assert stated.group(1) == declared, (
		f"pyproject.toml publishes {declared!r} and LICENSE is {stated.group(1)!r} — an index "
		f"would show one and a reader would open the other"
	)


def test_the_licence_file_the_package_points_at_exists () -> None:
	"""`license-files` is a glob, and a glob that matches nothing is not an error to hatchling."""

	loaded = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
	named = loaded["project"]["license-files"]

	assert named, "the package ships no licence file"

	for pattern in named:
		assert list(ROOT.glob(pattern)), f"license-files names {pattern!r} and nothing matches it"


@pytest.mark.parametrize("path", sorted(STATES_THE_LICENCE))
def test_every_file_that_states_the_licence_names_the_one_we_ship_under (path: str) -> None:
	"""**A licence change is one edit and a dozen sentences, and the sentences are what rot.**

	Derived from `pyproject.toml` rather than pinned to a string, so changing the licence fails
	here until every reader has been told — which is the whole of what went wrong when this one
	changed: fourteen files named the old licence and none of them was reachable from any test.

	**It looks for the statement, not for the string, and that distinction was earned.** The
	first version asked only whether the identifier appeared anywhere in the file. Falsifying it
	by reverting the README's licence section to the old licence **left it green**, because the
	summary line near the top names the licence too — so the file could say `AGPL` in the one
	place a reader goes for it and pass. A name that appears twice makes a substring check
	vacuous, which is `SR#405`'s lesson arriving in a new disguise.

	So the anchor is how a licence is actually stated: after *"under"*, or as the text of a link
	to `LICENSE`. Both forms appear in the tree, both are derived from the declared value, and
	neither matches a mention in passing.

	**Contradiction is still only caught where it displaces the statement**, deliberately.
	Several of these files say — correctly — what the AGPL used to require and no longer does,
	and `README` records that releases up to 0.5.0 remain under it. A sweep refusing the old name
	outright would need an excuse list of permanent entries, and a guard whose every excuse is
	permanent can never fire.
	"""

	declared = _declared()
	text = (ROOT / path).read_text(encoding="utf-8")

	# After "under", or as the text of a link to the licence file. Anything else is the licence
	# being mentioned rather than declared.
	stated = re.search(rf"(?:under\s+|\[){re.escape(declared)}\b", text)

	assert stated is not None, (
		f"{path} — {STATES_THE_LICENCE[path]} — does not state that Subroutine is under "
		f"{declared!r}, which is what this package publishes. Either it still states the old "
		f"licence, or it stopped stating one at all"
	)


def test_the_files_that_state_the_licence_all_exist () -> None:
	"""The other direction: a file renamed out from under this list takes its check with it."""

	for path in STATES_THE_LICENCE:
		assert (ROOT / path).is_file(), f"{path} is listed here and is not in the repository"


def test_the_program_is_installed_under_both_names_it_publishes () -> None:
	"""`#752`. `subr` is what somebody types after the first day, and it must be the same program.

	**Two entry points at one target, rather than an alias inside the app.** A Typer alias would
	be a second command in the help output, which is what `ls` is deliberately hidden from being
	— *a synonym a reader can see in a command list is a second thing to decide about*. This is
	not in a command list: the choice is how much to type, not which command to run.

	So there is nothing to keep in step, and this asserts exactly that: **the same target**, not
	two that happen to work today.
	"""

	scripts = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"]

	assert set(scripts) == {"subroutine", "subr"}, (
		f"the package installs {sorted(scripts)}; the README teaches 'subroutine' and 'subr'"
	)

	assert len(set(scripts.values())) == 1, (
		f"the two names point at {sorted(set(scripts.values()))} — they are one program under "
		f"two names, so a second target is a second program that will drift"
	)


def test_the_short_name_is_named_where_every_reader_of_help_sees_it () -> None:
	"""`#499`: the channel that is guaranteed must name every channel that is not.

	`subr` is installed beside `subroutine` and is otherwise **undiscoverable** — it is in no
	command list, and nothing about typing `subroutine` suggests a shorter spelling exists. A
	capability nothing announces is one nobody has, which is the failure `#499` was written for
	when 9.5 KB of agent documentation turned out to be unreachable.

	`--help`'s epilog is what every reader of the help page gets, and `subroutine help` prints
	the same page (`#154`).

	**Read off the app rather than the source**, so a rewritten epilog that drops it fails here.

	**Matched on a word boundary, and the first version was not.** `"subr" in epilog` passes on
	an epilog that never mentions the short name at all, because the word *subroutine* contains
	it — so deleting the sentence left this green. Found by falsification; nothing else could
	have. It is this repository's most-repeated defect, met here inside a guard written about
	something being undiscoverable.
	"""

	epilog = subroutine.cli.main.app.info.epilog or ""

	assert re.search(r"\bsubr\b", epilog), (
		f"nothing in the help page's epilog names the short spelling, so a reader meets it "
		f"nowhere: {epilog!r}"
	)


#: Every surface that carries the one-line description, and what a reader is doing when they
#: meet it. `#731`.
#:
#: **`pyproject.toml` is the source and everything else is checked against it**, which is the
#: only form "derived rather than restated" can take here: a plugin manifest is static JSON that
#: can import nothing, and a README is prose. So the derivation lives in this test rather than in
#: the files, exactly as `#678` put `ROUTED_WORKSPACE_WORDS`' derivation in a test to keep
#: `addressing.py` free of HTTP.
#:
#: **The GitHub repository description is deliberately absent**, and it is the one surface no
#: test can reach — it lives on github.com, not in the tree. `#732` carries it, and it has to be
#: pasted by a person. Listing it here would be an entry that can never be satisfied.
CARRIES_THE_DESCRIPTION = {
	".claude-plugin/marketplace.json": "the line under the marketplace's name in Claude Code",
	"plugins/subroutine/.claude-plugin/plugin.json": "what a reader sees before installing it",
	"plugins/subroutine-remote/.claude-plugin/plugin.json": "the same, for the remote plugin",
}


def _described () -> str:
	"""Return the one line this package publishes about itself, from the source of truth."""

	loaded = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

	return str(loaded["project"]["description"])


def test_one_sentence_describes_this_product_on_every_surface_that_carries_one () -> None:
	"""`#731`. Two variants of the line were live at once, which is the first defect.

	The PyPI summary said one thing and Simon quoted another, and `#733` then measured the
	construction they shared — *X for people and AI agents* — as the phrase **six** competitors
	already use, with the closest one's line being ours with two words changed. A sentence that
	is not the same everywhere cannot be fixed once, and a sentence nobody checks drifts back.

	**Read from `pyproject.toml`, which is the summary PyPI actually publishes**, so this cannot
	pass by comparing two copies of a constant with each other.
	"""

	sentence = _described()

	# A floor, because every assertion below is satisfied by an empty string appearing everywhere.
	assert len(sentence.split()) >= 6, (
		f"the description read from pyproject.toml is {sentence!r}, which is too short to be "
		f"the sentence this is checking — the scan has probably stopped reading the right key"
	)

	for name, why in CARRIES_THE_DESCRIPTION.items():
		text = (ROOT / name).read_text(encoding="utf-8")

		assert sentence in text, (
			f"{name} does not carry the description pyproject.toml publishes — it is {why}, so "
			f"a reader meets a different sentence there. Expected to find: {sentence!r}"
		)

	# The README states it as its standfirst, which is the first line a person reads on both
	# GitHub and PyPI — the two surfaces `#716` is about.
	readme = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()

	assert readme[2] == f"**{sentence}**", (
		f"README.md's standfirst is {readme[2]!r} and the published description is "
		f"{sentence!r}. They are the same claim and must be the same words."
	)


def test_no_surface_still_carries_a_description_we_have_replaced () -> None:
	"""`#405`'s other direction: agreeing on the new line is not the same as dropping the old.

	The check above passes on a tree where every surface carries **both** — the new sentence
	appended and the old one left above it. That is precisely what a half-finished rename looks
	like, and it is the state this repository was in for the whole of 0.6.x: `#731` decided the
	noun on 9 August and the rejected line was still on four surfaces the next day.
	"""

	retired = "Project management for people and agents, in equal measure."
	found = [
		name
		for name in [*CARRIES_THE_DESCRIPTION, "README.md", "pyproject.toml"]
		if retired in (ROOT / name).read_text(encoding="utf-8")
	]

	assert not found, (
		f"{found} still carry {retired!r}, which `#731` replaced. A surface holding both the "
		f"old line and the new one reads as two products."
	)


#: Where a link in the README has to point so that it works on **both** surfaces GitHub and
#: PyPI render it on. Simon's decision, 2026-08-09: ``main`` rather than the tag, so one edit
#: never rots — at the cost of a reader on the 0.6.0 page being shown documentation for
#: unreleased code, which is the smaller of the two problems.
README_BASE = "https://github.com/simonholliday/subroutine/blob/main/"


def test_the_published_description_has_no_link_only_github_can_resolve () -> None:
	"""``README.md`` is the description, and PyPI gives it no base URL to resolve against.

	`#716`. ``pyproject.toml`` sets ``readme = "README.md"``, so the file ships verbatim in the
	wheel with ``Description-Content-Type: text/markdown`` — and PyPI renders it *without* the
	rewriting GitHub does. A relative ``docs/hosting.md`` therefore resolves against the
	project page's own address, which is nothing PyPI serves.

	**It is the only page most PyPI visitors ever see**, and four of the nine broken links were
	the documents somebody needs *before* installing anything: hosting, connecting, the licence
	and how to report a vulnerability. `#694` audited the page's prose and found two false
	claims; nothing had ever asked whether its links work where it is published.

	This is the `#446` ratchet shape, and it would have caught the fault before 0.5.0.
	"""

	readme = (ROOT / "README.md").read_text(encoding="utf-8")

	# Anything that is not already absolute, not a bare fragment and not a mailto: is a path in
	# this repository, and a path in this repository is what PyPI cannot resolve.
	relative = re.findall(r"\]\((?!https?://|#|mailto:)([^)]+)\)", readme)

	assert not relative, (
		f"README.md links to {sorted(set(relative))} relatively, and PyPI renders the "
		f"description with no base URL — so each resolves to a page it does not serve. "
		f"Write them as {README_BASE}<path>."
	)


def test_every_repository_link_in_the_description_names_a_file_that_exists () -> None:
	"""The other direction, without which the rule above is satisfied by pointing anywhere.

	An absolute URL cannot be checked by looking at the filesystem *unless* it is one of ours,
	and ours are exactly the ones worth checking: a link rewritten to
	``blob/main/docs/hosting.md`` is as broken as the relative one it replaced if the file has
	since been renamed, and it fails silently on a page nobody here loads.
	"""

	readme = (ROOT / "README.md").read_text(encoding="utf-8")
	named = re.findall(rf"\]\({re.escape(README_BASE)}([^)]+)\)", readme)

	assert named, "no repository links were found, so this is checking nothing"

	missing = sorted({
		target for target in named
		if not (ROOT / target.split("#", 1)[0]).exists()
	})

	assert not missing, (
		f"README.md points at {missing}, which are not in the repository — so the link is "
		f"broken on GitHub as well as on PyPI"
	)
