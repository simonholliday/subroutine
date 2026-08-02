"""Print the changelog section for one release, for GitHub's release page. Item ``#241``.

**The notes are the changelog, not a second account of the same release.** Generating them from
commit subjects would produce a different story from the one `CHANGELOG.md` tells, and the two
would drift the moment anybody edited either — so this reads the section that already exists and
refuses if there is not one.

**Refusing is the point.** The failure this rules out is a release published with empty notes:
nothing errors, the page simply says nothing, and nobody notices until somebody goes looking for
what changed. So a missing or empty section exits non-zero and the release job fails with it.
"""

import argparse
import pathlib
import re
import sys

#: Resolved from this file, because the script is run from wherever somebody is standing.
CHANGELOG = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def main (argv: list[str] | None = None) -> int:
	"""Print the section for ``version``, or say why there is not one."""

	parsed = _arguments(argv)
	version = parsed.version.removeprefix("v")

	section = _section_for(CHANGELOG.read_text(encoding="utf-8"), version)

	if section is None:
		print(
			f"{CHANGELOG.name} has no '## {version}' section, so this release would be "
			f"published with nothing to read.",
			file=sys.stderr,
		)

		return 1

	if not section.strip():
		print(f"{CHANGELOG.name}'s '## {version}' section is empty.", file=sys.stderr)

		return 1

	print(section.strip())

	return 0


def _arguments (argv: list[str] | None) -> argparse.Namespace:
	"""Read which release to print."""

	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("version", help="the release, as 1.2.3 or v1.2.3")

	return parser.parse_args(argv)


def _section_for (text: str, version: str) -> str | None:
	"""Return the body under ``## <version>``, or ``None`` if the file has no such heading.

	Matched on the version rather than taken from the top of the file. The topmost section is
	the right answer at the moment a release is cut and the wrong one every time afterwards —
	and this runs from a tag, which can be built again long after the next release has landed.
	"""

	heading = re.compile(rf"^##\s+{re.escape(version)}\b.*$", re.MULTILINE)
	found = heading.search(text)

	if found is None:
		return None

	rest = text[found.end() :]
	following = re.search(r"^##\s", rest, re.MULTILINE)

	return rest[: following.start()] if following else rest


if __name__ == "__main__":
	raise SystemExit(main())
