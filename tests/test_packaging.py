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
import tomllib

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
