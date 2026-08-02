"""Cut a release — one version, written everywhere it has to appear. Item ``#239``.

**The number is a judgement and stays a human's.** Nothing here guesses whether a change is a
patch or a minor; what it removes is the chance to decide 0.1.3 and then say it in only some of
the places. ``#234`` took ``pyproject.toml`` out of that list by deriving the package version
from the tag. Three remain and every one has to agree on a single commit:

1. the git tag, which is what the package version is built from;
2. ``plugin.json``, which Claude Code reads straight from a clone with no build step, so
   nothing can derive it;
3. the changelog heading, which is how a person finds out what they are upgrading into.

**Two of the first three releases needed a corrective commit for exactly this** — 0.1.0's
changelog still said *Unreleased*, and 0.1.2 was tagged with the manifest reading 0.1.1, which
CI refused. Neither was carelessness; both were one step in a sequence somebody was holding in
their head.

**It refuses rather than repairs.** A dirty tree, a tag that exists, a version that goes
backwards and a changelog with nothing unreleased in it are all reasons to stop and let a
person look, because each means the release is not the thing they think it is.

**It never pushes.** Publishing is outward-facing and belongs to whoever owns the repository;
this stops at a commit and a tag, and prints the two commands that finish the job.
"""

import argparse
import datetime
import json
import pathlib
import re
import subprocess
import sys

#: The repository, resolved from this file rather than from the working directory — the script
#: is run from wherever somebody happens to be standing.
ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The two files that carry a version. The changelog's heading and the plugin's manifest; the
#: tag is made rather than written, and `pyproject.toml` no longer has one at all (`#234`).
CHANGELOG = ROOT / "CHANGELOG.md"
PLUGIN = ROOT / "plugins" / "subroutine" / ".claude-plugin" / "plugin.json"

#: What a version may look like. Deliberately narrow: three numbers, optionally a pre-release
#: suffix. A tag is not the place to discover that somebody typed `v0.1.3` or `0.1` — the first
#: would produce `vv0.1.3` and the second a version PyPI sorts in a way nobody expects.
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?$")

#: The heading this promotes. **An `Unreleased` section has to already exist**, which is the
#: point rather than a limitation: it means the changelog was written while the work was fresh,
#: by whoever did it, instead of at the moment of release by whoever is in a hurry. The
#: changelog's own preamble already describes the file this way.
UNRELEASED = re.compile(r"^##\s+Unreleased\b.*$", re.IGNORECASE | re.MULTILINE)


def main (argv: list[str] | None = None) -> int:
	"""Write ``version`` into every place a release names itself, then commit and tag it."""

	parsed = _arguments(argv)
	version = parsed.version

	if not VERSION.match(version):
		return _refuse(f"{version!r} is not a version. Write it as 1.2.3, with no leading 'v'.")

	problem = _reasons_to_stop(version)

	if problem is not None:
		return _refuse(problem)

	on = parsed.date or datetime.date.today().isoformat()
	changelog = UNRELEASED.sub(f"## {version} — {on}", CHANGELOG.read_text(encoding="utf-8"), 1)

	if parsed.dry_run:
		print(f"Would release {version} ({on}):")
		print(f"  {CHANGELOG.name}: the Unreleased heading becomes '## {version} — {on}'")
		print(f"  {PLUGIN.name}: version becomes {version}")
		print(f"  commit both, then tag v{version}")

		return 0

	CHANGELOG.write_text(changelog, encoding="utf-8")
	_write_plugin_version(version)

	# **`check_release_notes.py` is deliberately not run here.** It compares this commit's
	# migration head against the head at the most recent tag — which is the same comparison CI
	# makes on every push to main, against the same previous tag. So a missing migration notice
	# is already refused before anybody reaches this script, and running it again would couple
	# two scripts for an answer that has been available since the commit that moved the head.
	_git("add", str(CHANGELOG), str(PLUGIN))
	_git("commit", "-m", f"Release {version}", "-m", f"See CHANGELOG.md for what {version} contains.")
	_git("tag", "-a", f"v{version}", "-m", f"Subroutine {version}")

	print(f"Committed and tagged v{version}. Nothing has been pushed. To publish:")
	print("  git push")
	print(f"  git push origin v{version}")

	return 0


def _arguments (argv: list[str] | None) -> argparse.Namespace:
	"""Read the version to cut, and the two options that exist for testing it."""

	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("version", help="the release, as 1.2.3, with no leading 'v'")
	parser.add_argument(
		"--dry-run", action="store_true", help="say what would change and touch nothing"
	)

	# Passed in rather than always read off the clock, so a test can assert the heading it
	# expects instead of building today's date twice and comparing a program to itself.
	parser.add_argument("--date", help="the date to stamp, as YYYY-MM-DD. Defaults to today.")

	return parser.parse_args(argv)


def _reasons_to_stop (version: str) -> str | None:
	"""Return why this release must not be cut, or ``None`` if there is no reason."""

	if _git("status", "--porcelain").strip():
		return "the working tree has changes. Commit or stash them, so the tag names a state."

	if _git("tag", "--list", f"v{version}").strip():
		return f"v{version} already exists. A released version is never re-cut — choose the next one."

	waiting = _unreleased(CHANGELOG.read_text(encoding="utf-8"))

	if waiting is None:
		return (
			f"{CHANGELOG.name} has no '## Unreleased' section, so this release would say "
			f"nothing about itself. Write what changed under one, then cut it."
		)

	# **Empty is caught here rather than at the announcement** (`#243`). `release_notes.py`
	# refuses an empty section too, but that runs in the job *after* the upload — so a heading
	# with nothing under it used to pass this, publish to PyPI, and then fail on the way to the
	# GitHub release, having already spent a version number that cannot be reused.
	if not waiting.strip():
		return (
			f"{CHANGELOG.name}'s '## Unreleased' section is empty. Write what changed under "
			f"it — a release nobody can read about is worse than one nobody cut."
		)

	latest = _latest_version()
	proposed = _ordered(version)
	previous = _ordered(latest) if latest is not None else None

	# **Only when both parse as plain numbers.** A pre-release suffix makes ordering a question
	# with more than one defensible answer, and guessing it here would refuse a release
	# somebody meant. Refusing what is unambiguously backwards is the whole value.
	if proposed is not None and previous is not None and proposed <= previous:
		return f"{version} is not ahead of {latest}, the most recent tag."

	return None


def _unreleased (text: str) -> str | None:
	"""Return what is written under ``## Unreleased``, or ``None`` if there is no such heading.

	The empty string is a real answer and a different one from ``None`` — a heading with
	nothing under it and no heading at all fail for different reasons and deserve to be told
	apart, because the first looks like somebody started and the second like nobody did.
	"""

	found = UNRELEASED.search(text)

	if found is None:
		return None

	rest = text[found.end() :]
	following = re.search(r"^##\s", rest, re.MULTILINE)

	return rest[: following.start()] if following else rest


def _ordered (version: str) -> tuple[int, ...] | None:
	"""Return a version as numbers for comparison, or ``None`` if it is not purely numeric."""

	parts = version.split(".")

	if len(parts) != 3 or not all(part.isdigit() for part in parts):
		return None

	return tuple(int(part) for part in parts)


def _latest_version () -> str | None:
	"""Return the newest release tag with its ``v`` removed, or ``None`` on a repository with none."""

	found = subprocess.run(
		["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
		cwd=ROOT, capture_output=True, text=True, check=False,
	)

	if found.returncode != 0 or not found.stdout.strip():
		return None

	return found.stdout.strip().removeprefix("v")


def _write_plugin_version (version: str) -> None:
	"""Set the plugin manifest's version, leaving every other key and the formatting alone.

	Rewritten through ``json`` rather than by pattern, because a manifest that stops being
	valid JSON is one nobody can install and the failure arrives at a stranger.
	"""

	manifest = json.loads(PLUGIN.read_text(encoding="utf-8"))
	manifest["version"] = version

	PLUGIN.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _git (*arguments: str) -> str:
	"""Run one git command in the repository and return its output, raising if it fails."""

	return subprocess.run(
		["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=True
	).stdout


def _refuse (message: str) -> int:
	"""Say why the release is not being cut, and exit non-zero."""

	print(f"Not releasing: {message}", file=sys.stderr)

	return 1


if __name__ == "__main__":
	raise SystemExit(main())
