"""Write ``docs/errors.md`` from the error registry — item ``#2327``.

**The page had a guard and no command.** ``tests/test_errors.py`` fails the moment the page and
``subroutine.errors`` disagree, which is the best drift check this repository has, and what it
asked for was a person pasting a function's output into a file. A check that is satisfied by a
paste is a check that gets skipped; this is the one step that pipeline was missing.

Run it after changing the registry:

    python scripts/errors_page.py
"""

import pathlib
import sys

import subroutine.errors

#: Resolved from this file, because the script is run from wherever somebody is standing.
PAGE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "errors.md"


def main (page: pathlib.Path = PAGE) -> int:
	"""Write the page, saying whether anything changed.

	``page`` is an argument so a test can hand it a file of its own, and never the real one.
	"""

	wanted = subroutine.errors.registry_markdown()

	if page.is_file() and page.read_text(encoding="utf-8") == wanted:
		print(f"{page.name} is already current.")

		return 0

	page.write_text(wanted, encoding="utf-8")
	print(f"Wrote {page.name} from the {len(subroutine.errors.REGISTRY)} codes in the registry.")

	return 0


if __name__ == "__main__":
	sys.exit(main())
