"""Fail if a release changes the database schema without saying so in the changelog.

SPEC.md §12.4a and decision ``#97``. **The user should plan a database upgrade, not discover
one halfway through.** An upgrade that needs a migration means taking a backup, accepting a
short outage and running a command — all fine when it is on the page in front of somebody, and
all unwelcome at the point they have already stopped the service.

**Derived, never remembered.** The migration directory knows whether the head moved between two
tags, so nothing here depends on anybody noticing at release time — which is exactly when
nobody does. A rule that holds only when the release is unhurried is not a rule.

Run by CI, and by hand:

    python scripts/check_release_notes.py            # against the most recent tag
    python scripts/check_release_notes.py --emit     # print the notice, ready to paste

One asymmetry in here is deliberate and load-bearing. The head *now* is read from Alembic
itself; the head *then* is parsed out of the files as they were at the tag. Parsing both would
mean a bug in the parser producing the same wrong answer twice, the two comparing equal, and
the check concluding that no migration is carried — a wrong answer in the direction that lets a
release ship without its warning. Getting it wrong the other way costs somebody a notice they
did not need.
"""

import argparse
import pathlib
import re
import subprocess
import sys

import subroutine.db.migrate

#: Where migrations live, relative to the repository root. Read out of git rather than off
#: disk, so the question "what was the head at that tag" can be asked at all.
VERSIONS = "src/subroutine/db/migrations/versions"

#: The changelog, whose topmost ``## `` section is taken to be the release being prepared.
#: Topmost rather than "the section matching the version in pyproject.toml": the two would be
#: a second thing to keep in step, and the answer to "what is about to ship" is the top of the
#: file in every changelog anybody writes.
CHANGELOG = pathlib.Path("CHANGELOG.md")

#: Alembic's generated header, which every one of these files carries because they all come
#: from one template. The annotation is optional in the pattern so that a file written before
#: the current template still parses.
_REVISION = re.compile(r"^revision(?:\s*:[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_DOWN = re.compile(r"^down_revision(?:\s*:[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)

#: What the notice must contain. Three separate things, because each catches a different way of
#: getting it wrong: the phrase catches a release that says nothing, the command catches a
#: warning that does not say what to do, and the new revision catches a notice copied forward
#: from the previous release and left stale — which is the one a human review would miss.
REQUIRED_PHRASE = "changes the database schema"
REQUIRED_COMMAND = "subroutine db upgrade"


def main (argv: list[str] | None = None) -> int:
	"""Compare the schema head against a previous tag and check the changelog says so.

	``argv`` is taken as an argument rather than read off :data:`sys.argv` inside, so that the
	whole of this can be driven from a test. A gate that can only be exercised by being run for
	real is a gate nobody exercises until the release it was supposed to protect.
	"""

	parsed = _arguments(argv)
	now = subroutine.db.migrate.head_revision()

	if now is None:
		print("No migrations found in this checkout — that cannot be right.", file=sys.stderr)

		return 1

	if parsed.emit:
		print(_notice(_head_at(parsed.since) if parsed.since else None, now))

		return 0

	since = parsed.since or _most_recent_tag()

	if since is None:
		# Nothing to upgrade *from*, so there is no migration to advertise. A fresh install
		# runs `init`, which builds the schema outright.
		print("No previous tag, so this is the first release. Nothing to compare against.")

		return 0

	before = _head_at(since)

	if before == now:
		print(f"Schema head is unchanged since {since} ({now}). No notice needed.")

		return 0

	# Flushed, because everything after this goes to standard error and an unflushed pipe
	# delivers the finding before the fact it was drawn from.
	print(f"Schema head moved since {since}: {before} -> {now}.", flush=True)

	return _check_changelog(now)


def _check_changelog (now: str) -> int:
	"""Report whether the release being prepared carries the migration notice."""

	if not CHANGELOG.is_file():
		print(f"{CHANGELOG} does not exist, and this release needs a notice in it.", file=sys.stderr)

		return 1

	section = _first_section(CHANGELOG.read_text(encoding="utf-8"))

	if section is None:
		print(f"{CHANGELOG} has no '## ' section to put the notice in.", file=sys.stderr)

		return 1

	title, body = section
	missing = [
		description
		for description, present in (
			(f"the phrase {REQUIRED_PHRASE!r}", REQUIRED_PHRASE in body),
			(f"the command '{REQUIRED_COMMAND}'", REQUIRED_COMMAND in body),
			(f"the new revision {now}", now in body),
		)
		if not present
	]

	if not missing:
		print(f"'{title}' carries the migration notice.")

		return 0

	print(
		f"This release changes the schema, and '{title}' in {CHANGELOG} is missing "
		f"{', and '.join(missing)}.",
		file=sys.stderr,
	)
	print("Run 'python scripts/check_release_notes.py --emit' for the wording.", file=sys.stderr)

	return 1


def _notice (before: str | None, now: str) -> str:
	"""Return the warning itself, ready to paste under the release heading.

	The three things somebody needs in order to *plan* rather than react: that it happens at
	all, what it costs, and the one command that does it safely. The revisions are in it
	because they are what ``subroutine db upgrade`` and ``subroutine --version`` print, so the
	notice and the program say the same words.
	"""

	moved = f"from `{before}` to `{now}`" if before is not None else f"to `{now}`"

	return (
		f"> **This release {REQUIRED_PHRASE}**, {moved}.\n"
		f">\n"
		f"> Install it, then run `{REQUIRED_COMMAND}`. That reports both versions, takes a\n"
		f"> verified backup, migrates and checks the result — in that order. Stop the service\n"
		f"> first if you are running one; expect it to be down for the length of the migration."
	)


def _first_section (text: str) -> tuple[str, str] | None:
	"""Return the topmost ``## `` heading and the lines under it."""

	lines = text.splitlines()

	for index, line in enumerate(lines):
		if not line.startswith("## "):
			continue

		rest = lines[index + 1 :]
		ends = next(
			(offset for offset, later in enumerate(rest) if later.startswith("## ")), len(rest)
		)

		return line[3:].strip(), "\n".join(rest[:ends])

	return None


def _head_at (ref: str) -> str:
	"""Return the schema head as it was at a git ref.

	Raises rather than guessing. Every failure here — an unknown ref, a directory with no
	migrations in it, a chain with two ends — means this cannot answer the question, and a
	release check that shrugs is one that passes on the day it matters.
	"""

	listed = _git("ls-tree", "-r", "--name-only", ref, "--", VERSIONS)
	paths = [
		path
		for path in listed.splitlines()
		if path.endswith(".py") and not path.endswith("__init__.py")
	]

	if not paths:
		raise SystemExit(f"No migrations found at {ref}. Is {VERSIONS} the right path?")

	revisions = {}

	for path in paths:
		body = _git("show", f"{ref}:{path}")
		found = _REVISION.search(body)

		if found is None:
			raise SystemExit(f"Could not read a revision id out of {path} at {ref}.")

		below = _DOWN.search(body)
		revisions[found.group(1)] = None if below is None else below.group(1)

	ends = set(revisions) - {below for below in revisions.values() if below is not None}

	if len(ends) != 1:
		raise SystemExit(
			f"Expected exactly one head at {ref} and found {len(ends)}: {sorted(ends)}. "
			f"A branched migration history needs a person to look at it."
		)

	return ends.pop()


def _most_recent_tag () -> str | None:
	"""Return the newest tag reachable from HEAD, or ``None`` if there are none yet."""

	found = subprocess.run(
		["git", "describe", "--tags", "--abbrev=0"],
		capture_output=True,
		text=True,
		check=False,
	)

	return found.stdout.strip() or None


def _git (*arguments: str) -> str:
	"""Run one read-only git command and return its output."""

	found = subprocess.run(
		["git", *arguments], capture_output=True, text=True, check=False
	)

	if found.returncode != 0:
		raise SystemExit(f"git {' '.join(arguments)} failed: {found.stderr.strip()}")

	return found.stdout


def _arguments (argv: list[str] | None) -> argparse.Namespace:
	"""Parse the command line."""

	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument(
		"--since",
		default=None,
		help="The tag or commit to compare against. Defaults to the most recent tag.",
	)
	parser.add_argument(
		"--emit",
		action="store_true",
		help="Print the notice for the current head and stop.",
	)

	return parser.parse_args(argv)


if __name__ == "__main__":
	sys.exit(main())
