"""Write ``docs/permissions.md`` from the act table and the seeded roles - item ``#4579``.

The page ``docs/design.md`` section 7 promised and nothing produced: each act, the permission it
asks and where, written from :data:`subroutine.acts.ACTS` - the acts the permission matrix drives
through every way in - and the roles every workspace is given. ``tests/test_permissions_page.py``
fails when the page and the code disagree, as ``docs/errors.md``'s guard does.

Run it after changing an act or a role:

    python scripts/permissions_page.py
"""

import pathlib
import sys

import subroutine.acts

#: Resolved from this file, because the script is run from wherever somebody is standing.
PAGE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "permissions.md"


def main (page: pathlib.Path = PAGE) -> int:
	"""Write the page, saying whether anything changed.

	``page`` is an argument so a test can hand it a file of its own, and never the real one.
	"""

	wanted = subroutine.acts.page_markdown()

	if page.is_file() and page.read_text(encoding="utf-8") == wanted:
		print(f"{page.name} is already current.")

		return 0

	page.write_text(wanted, encoding="utf-8")
	print(f"Wrote {page.name} from the {len(subroutine.acts.ACTS)} acts in the table.")

	return 0


if __name__ == "__main__":
	sys.exit(main())
