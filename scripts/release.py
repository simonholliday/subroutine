"""Cut a release — one version, written everywhere it has to appear. Item ``#239``.

**The number is a judgement and stays a human's.** Nothing here guesses whether a change is a
patch or a minor; what it removes is the chance to decide 0.1.3 and then say it in only some of
the places. ``#234`` took ``pyproject.toml`` out of that list by deriving the package version
from the tag. What is left has to agree on a single commit:

1. the git tag, which is what the package version is built from;
2. **every** ``plugin.json``, which Claude Code reads straight from a clone with no build step,
   so nothing can derive it — and there is more than one since `#540`, which is why
   :data:`PLUGINS` is discovered rather than listed;
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

import subroutine.db.migrate
import subroutine.installations

#: The repository, resolved from this file rather than from the working directory — the script
#: is run from wherever somebody happens to be standing.
ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The files that carry a version. The changelog's heading and every plugin manifest; the tag is
#: made rather than written, and `pyproject.toml` no longer has one at all (`#234`).
CHANGELOG = ROOT / "CHANGELOG.md"

#: **Discovered rather than listed** (`#540`). Two plugins ship from this repository and a third
#: is a directory away, so naming them here would mean a release that silently left one behind —
#: at a version somebody already has cached, which is `#380`'s failure with nobody to notice it.
#: Sorted, so the dry run and the commit list them in the same order every time.
PLUGINS = tuple(
	sorted(
		path / ".claude-plugin" / "plugin.json"
		for path in (ROOT / "plugins").iterdir()
		if (path / ".claude-plugin" / "plugin.json").is_file()
	)
)

#: The plugin server definitions that bootstrap through ``uvx`` and so carry a version pin.
#: Discovered the same way :data:`PLUGINS` is and for the same reason, and filtered by *reading*
#: rather than by naming: a plugin reaching an instance over HTTP has no package to pin, and one
#: added later that does will be found without this line being touched.
BOOTSTRAPS = tuple(
	sorted(
		path
		for path in (ROOT / "plugins").glob("*/.mcp.json")
		if "subroutine~=" in path.read_text(encoding="utf-8")
	)
)

#: The record `subroutine db upgrade --check` reads — item `#321`. **Written here rather than
#: derived by whoever asks**, because the fact it carries is only knowable at the moment of
#: release: the schema head this version expects. PyPI publishes a version and nothing about a
#: database, so without this an operator can be told a release exists and not whether taking it
#: means stopping the service.
#:
#: Newest first, matching the changelog, so the file *is* the ordering and nothing that reads
#: it has to compare version strings.
RELEASES = ROOT / "docs" / "releases.json"

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

	head = subroutine.db.migrate.head_revision()

	if head is None:
		return _refuse("no migration head could be read, so the release record cannot say what "
		               "schema this version expects.")

	major, minor, *_ = version.split(".")
	pinned = PIN.format(major=major, minor=minor)

	if parsed.dry_run:
		print(f"Would release {version} ({on}):")
		print(f"  {CHANGELOG.name}: the Unreleased heading becomes '## {version} — {on}'")
		for manifest in PLUGINS:
			print(f"  {manifest.parent.parent.name}: version becomes {version}")
		for bootstrap in BOOTSTRAPS:
			print(f"  {bootstrap.parent.name}: uvx is pointed at '{pinned}'")
		print(f"  {RELEASES.name}: {version} recorded at schema {head}")
		print(f"  commit all {2 + len(PLUGINS) + len(BOOTSTRAPS)}, then tag v{version}")

		return 0

	CHANGELOG.write_text(changelog, encoding="utf-8")
	_write_plugin_version(version)
	_write_uvx_pin(version)
	_record_release(version, head, on)

	# **`check_release_notes.py` is deliberately not run here.** It compares this commit's
	# migration head against the head at the most recent tag — which is the same comparison CI
	# makes on every push to main, against the same previous tag. So a missing migration notice
	# is already refused before anybody reaches this script, and running it again would couple
	# two scripts for an answer that has been available since the commit that moved the head.
	_git(
		"add", str(CHANGELOG), str(RELEASES),
		*(str(path) for path in PLUGINS), *(str(path) for path in BOOTSTRAPS),
	)
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
	proposed = subroutine.installations.ordered(version)
	previous = subroutine.installations.ordered(latest) if latest is not None else None

	# **Only when both parse as plain numbers.** A pre-release suffix makes ordering a question
	# with more than one defensible answer, and guessing it here would refuse a release
	# somebody meant. Refusing what is unambiguously backwards is the whole value.
	if proposed is not None and previous is not None and proposed <= previous:
		return f"{version} is not ahead of {latest}, the most recent tag."

	# **The plugin's version leads, and a release may not walk it backwards** (`#396`).
	#
	# The two numbers do different jobs at different frequencies. The plugin's is a *cache
	# key*: Claude Code stores an installed copy under it, so it must move on any change to
	# `plugins/` or the artefact cannot be delivered at all — met twice, as `#380` and `#393`.
	# The package's is a *release*, which is an act with a changelog behind it. Tagging every
	# plugin bump would make a release mean nothing.
	#
	# So the manifest runs ahead between releases and the next release takes the number it has
	# reached. A skipped package version is cheap; **cutting below the manifest would publish a
	# plugin version somebody already has cached**, which is `#380` with the numbers reversed
	# and no way for anybody to notice.
	#
	# Equal is the ordinary case and is fine: the manifest reaching the number first is exactly
	# how this is meant to work.
	# **The highest of them, because a cache key is per plugin.** One manifest ahead of the
	# others is the ordinary state — a change under one plugin bumps only that one — so the
	# floor is whichever has travelled furthest, or the release could never reach its users.
	for manifest in PLUGINS:
		declared = json.loads(manifest.read_text(encoding="utf-8"))["version"]
		carried = subroutine.installations.ordered(declared)

		if proposed is not None and carried is not None and proposed < carried:
			return (
				f"{version} is behind {declared}, which {manifest.parent.parent.name} already "
				f"carries. Anybody who installed {declared} has it cached under that number, "
				f"so a release below it could never reach them. Cut {declared} or higher."
			)

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


def _latest_version () -> str | None:
	"""Return the newest release tag with its ``v`` removed, or ``None`` on a repository with none."""

	found = subprocess.run(
		["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
		cwd=ROOT, capture_output=True, text=True, check=False,
	)

	if found.returncode != 0 or not found.stdout.strip():
		return None

	return found.stdout.strip().removeprefix("v")


def _record_release (version: str, head: str, on: str) -> None:
	"""Put this release at the top of the published record.

	**At the top rather than sorted in**, because the file is read as an ordering and a
	release is always the newest thing in it — `_reasons_to_stop` has already refused anything
	that is not ahead of the most recent tag. Sorting would mean parsing versions, which is
	the arithmetic `subroutine.releases` exists without.
	"""

	record = json.loads(RELEASES.read_text(encoding="utf-8")) if RELEASES.is_file() else {}
	rows = record.get("releases", [])

	if any(row.get("version") == version for row in rows):
		raise SystemExit(f"{RELEASES.name} already records {version}.")

	record["releases"] = [{"version": version, "schema": head, "date": on}, *rows]

	# The directory may not be there on a fork cutting its first release, and refusing for
	# that would be a release tool stopping on something it can fix.
	RELEASES.parent.mkdir(parents=True, exist_ok=True)
	RELEASES.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def _write_plugin_version (version: str) -> None:
	"""Set every plugin manifest's version, leaving all other keys and the formatting alone.

	**All of them, to the same number.** They may sit at different versions between releases —
	each is a cache key for its own plugin — but a release is one act and one tag, so a manifest
	left behind is a plugin whose users are told a version exists that they cannot install.

	**And this docstring used to be false, which is how `#749` shipped.** It said the formatting
	was left alone while the body parsed the document and wrote it back through ``json.dumps`` —
	so every em dash in a description became ``\\u2014`` on every release, and a manifest already
	at the release number was rewritten anyway. That churn was invisible for as long as the
	version also moved, and `v0.6.2` was the release where it did not: the only change under
	``plugins/`` was the escaping, the version could not move to announce it, and
	``test_a_changed_plugin_carries_a_version_nobody_has_installed`` failed the tag. Both
	workflows went red and nothing published.
	"""

	for path in PLUGINS:
		_set_json_string(path, "version", version)


def _set_json_string (path: pathlib.Path, key: str, value: str) -> None:
	"""Set one top-level string in a JSON file, writing nothing if it is already that.

	**Edited textually rather than round-tripped, and that is the whole of `#749`.** Parsing a
	document and writing it back re-formats everything it did not come for: ``json.dumps``
	escapes non-ASCII by default, so a description written with an em dash comes back as
	``\\u2014``, and no serialiser preserves a human's line layout. Those diffs mean nothing to a
	reader and everything to a guard — ``test_a_changed_plugin_carries_a_version_nobody_has_installed``
	reads a change under ``plugins/`` as a plugin somebody's cache will never receive, correctly.

	**Parsed first anyway, for the reason the old body gave**: a manifest that stops being valid
	JSON is one nobody can install, and the failure arrives at a stranger. So this parses to
	decide, edits the text, and parses again to prove the result still says what was intended.

	**Nothing is written when the value is already right.** A release is entitled to be a no-op
	on a file it is not changing, and making that structural is what stops the next unrelated
	formatting habit reintroducing this.
	"""

	text = path.read_text(encoding="utf-8")
	current = json.loads(text)[key]

	if current == value:
		return

	_replace_json_value(path, current, value, key=key)


def _replace_json_value (
	path: pathlib.Path, current: str, value: str, key: str | None = None
) -> None:
	"""Replace one JSON string value in place, leaving every other byte as it was.

	Matched with its quotes, and with its key too when there is one, so the pattern cannot land
	on a substring of something else. **A count other than one is refused** rather than guessed
	at: this edits a published artefact, and a second occurrence means the assumption that
	makes a textual edit safe has stopped holding.
	"""

	text = path.read_text(encoding="utf-8")
	target = f'"{key}": "{current}"' if key else f'"{current}"'
	found = text.count(target)

	if found != 1:
		raise SystemExit(
			f"{path} holds {found} occurrences of {target}, and this edits it by text — so "
			f"one is the only count it can act on. Change it by hand, or give the writer a "
			f"narrower pattern."
		)

	written = text.replace(target, f'"{key}": "{value}"' if key else f'"{value}"')

	# Proof rather than trust: the result must still parse, and must now say what was asked.
	reread = json.loads(written)

	if key is not None and reread[key] != value:
		raise SystemExit(f"{path}: setting {key!r} to {value!r} did not take.")

	path.write_text(written, encoding="utf-8")


#: How a plugin asks ``uvx`` for the program: the package, pinned to the release series it was
#: published beside. ``~=`` is a compatible release — ``~=0.6.0`` is ``>=0.6.0, ==0.6.*`` — so a
#: user picks up fixes without being moved to a minor version that may carry a migration.
PIN = "subroutine~={major}.{minor}.0"


def _write_uvx_pin (version: str) -> None:
	"""Point every ``uvx`` bootstrap at the series being released.

	**This exists because ``uvx`` floats and a local instance cannot afford that** (`#585`).
	``uvx subroutine`` resolves to whatever is newest whenever the cache next looks, so an
	unpinned bootstrap changes the code running against somebody's SQLite database on a day
	they did not choose — and a minor version is exactly where a migration lands. Pinned to
	``~=X.Y.0`` they get patches and nothing that moves the schema.

	**Not pinned to the manifest's own version**, which is the tempting mistake: a manifest is a
	cache key and leads the package between releases (`#396`), so a plugin at 0.6.1 beside a
	published 0.6.0 would ask PyPI for something that does not exist.

	Discovered from the filesystem like :data:`PLUGINS`, so a plugin added later is pinned
	without being named here — and one that bootstraps some other way is left alone, because
	only an argument that already looks like this pin is rewritten.
	"""

	major, minor, *_ = version.split(".")
	wanted = PIN.format(major=major, minor=minor)

	for path in BOOTSTRAPS:
		servers = json.loads(path.read_text(encoding="utf-8"))
		present = {
			argument
			for server in servers.get("mcpServers", {}).values()
			for argument in (server.get("args") or [])
			if argument.startswith("subroutine~=")
		}

		# **A bootstrap with no pin is left alone entirely** — the remote plugin needs no
		# package, and rewriting its file to change nothing is what `#749` was.
		for pin in present:
			if pin != wanted:
				_replace_json_value(path, pin, wanted)


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
