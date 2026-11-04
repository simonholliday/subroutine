"""What 1.0 promises is said where a stranger reads it, and no published page says otherwise - `SR#5263`.

Found scanning for what 1.0 owes before the tag. PyPI's page said *Alpha*, `SECURITY.md` said
*`0.x`: it is early, the surface is still moving*, and the promise itself was written nowhere a stranger
reads. Each was true when written and false by the number alone, so no test failed and
`scripts/release.py`, which touches none of them, could not notice.
"""

import pathlib
import re

import test_packaging

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: What a reader must be able to find in the promise: the surfaces `#3715` names, the rule they
#: follow, and the answer of `#5264` about a program and an instance on different releases.
PROMISED = (
	"`/v1`",
	"commands, flags and exit codes",
	"error codes",
	"agent tools and resources",
	"plugin settings",
	"your data",
	"semantic versioning",
	"A newer program still reads an older instance",
)

#: What only a release before 1.0 could say about itself.
EARLY = re.compile(r"`0\.x`|\bis early\b|still moving|\b[Aa]lpha\b|\b[Bb]eta\b")


def _published () -> list[pathlib.Path]:
	"""Return the pages a stranger reads, less the frozen specification, which records its day."""

	pages = [ROOT / name for name in ("README.md", "SECURITY.md", "CONTRIBUTING.md")]

	return pages + [one for one in sorted((ROOT / "docs").glob("*.md")) if one.name != "design.md"]


def _says_it_is_early (text: str) -> list[str]:
	"""Return each line of a page that speaks as a release before 1.0 would."""

	return [line for line in text.splitlines() if EARLY.search(line)]


def test_the_package_says_it_is_stable () -> None:
	"""PyPI shows the classifier on the release's own page, beside the promise it would contradict."""

	status = [one for one in test_packaging._classifiers() if one.startswith("Development Status")]

	assert status == ["Development Status :: 5 - Production/Stable"], status


def test_the_readme_says_what_stays_put () -> None:
	"""The promise is the point of 1.0, so a reader can find it before installing anything."""

	readme = (ROOT / "README.md").read_text(encoding="utf-8")
	start = readme.index("## What stays put")
	section = readme[start:readme.index("\n## ", start + 1)]
	flowing = " ".join(section.split())
	missing = [one for one in PROMISED if one not in flowing]

	assert not missing, f"the promise no longer says {missing}: {flowing}"


def test_the_readme_and_contributing_agree_about_pull_requests () -> None:
	"""Decided in `#5266`: they disagreed from 2026-09-21, one turning down what the other invited.

	Both directions, so opening pull requests later has to change both pages together.
	"""

	readme = (ROOT / "README.md").read_text(encoding="utf-8")
	section = readme[readme.index("## Contributing"):readme.index("## Licence")]
	contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
	seeking = "not seeking code contributions yet"

	assert (seeking in section) == (seeking in contributing), section
	assert ("Pull requests are welcome" in section) != (seeking in contributing), section


def test_no_published_page_calls_it_early () -> None:
	"""`SECURITY.md` was the one that did. **At least the three pages named**, so a walk reading
	nothing cannot pass."""

	pages = _published()
	found = {
		page.name: _says_it_is_early(page.read_text(encoding="utf-8")) for page in pages
	}

	assert len(pages) >= 3
	assert all(page.exists() for page in pages)
	assert not any(found.values()), {name: lines for name, lines in found.items() if lines}


def test_the_check_fires_on_what_the_page_used_to_say () -> None:
	"""Fed the sentence it exists to catch, through its own entry point."""

	said = "the only version that gets a fix. Subroutine is `0.x`: it\nis early, the surface moves"

	assert _says_it_is_early(said) == [
		"the only version that gets a fix. Subroutine is `0.x`: it", "is early, the surface moves",
	]
	assert _says_it_is_early("From 1.0 Subroutine follows semantic versioning.") == []
