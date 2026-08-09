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
