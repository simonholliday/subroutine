"""What a *checkout* belongs to — SPEC.md §13.7a, item ``#159``.

**The question an agent could not answer.** §21.5's adoption procedure creates a project for a
repository, and then every later session has to work out which of an instance's projects this
directory corresponds to. On one project that is free. On twenty — which is what adoption
*produces* — the agent guessed from the directory name, and a guess that is usually right is
the worst kind: it files work into the wrong project rarely enough that nobody is watching.

**A file in the tree, found by walking up, exactly as git finds `.git`.** That is the only
mechanism that survives the things people actually do: a subdirectory, a worktree, an agent
started from somewhere else, two checkouts of the same repository at different paths, and two
different repositories open at once. A machine-global setting — which is what ``subroutine
use`` writes — cannot answer a per-directory question, and asking the user every session is
the interview §21.5 exists to avoid.

**Three decisions worth keeping.**

- **A project *key*, not an id.** ``SR`` is readable, is stable because §5.2 forbids renaming
  one, and can be checked by a person reading the file. A UUID is noise nobody can verify, and
  the failure mode of a wrong one is silence.
- **Not in ``CLAUDE.md``.** That file is context every session carries (`#64`), it is specific
  to one vendor's agent, and nothing else reads it. This is read by the CLI, by MCP, and by
  whatever comes next.
- **It names the connection and workspace too**, because a freelancer with an instance of
  their own and a month on somebody else's needs all three to be unambiguous — and those two
  keys are spelled exactly as ``context.toml`` spells them, so there is one vocabulary rather
  than two.

**It is safe to lose, and that is the same property `context.py` argues for.** Deleting it
means new items go where they went before this existed: the current context, and the Inbox.
Nothing becomes unreachable and no identifier changes meaning. The test any addition here must
pass: losing this file may cost a question, never a different outcome that nobody sees.

**Which is why the surfaces say when they used it.** A file found three directories up that
silently redirects where work is filed would be exactly the footgun `context.py` calls the
standing one in comparable tooling — not having a setting, but not knowing where it came from.
"""

import pathlib
import tomllib
import typing

#: What the file is called. A dotfile because it is machine-written configuration rather than
#: something a reader of the repository needs to meet, and TOML because every other file this
#: program writes is — one parser, one set of quoting rules, one thing to explain.
FILE_NAME = ".subroutine"

#: The keys it may carry. ``connection`` and ``workspace`` are spelled as ``context.toml``
#: spells them on purpose; ``project`` is the one this file adds, and the reason it exists.
KEYS = ("connection", "workspace", "project")


class Marker(typing.NamedTuple):
	"""What a directory says it belongs to, and where that was written down."""

	path: pathlib.Path
	connection: str | None = None
	workspace: str | None = None
	project: str | None = None

	def describe (self) -> str:
		"""Return how a person reads this — ``SR, from ../.subroutine``."""

		named = "/".join(
			part for part in (self.connection, self.workspace, self.project) if part
		)

		return f"{named or 'nothing'}, from {self.path}"


def find (start: pathlib.Path | None = None) -> Marker | None:
	"""Return the nearest marker at or above ``start``, or ``None``.

	**The nearest wins and the walk stops there.** A repository inside another repository is
	rare and deliberate when it happens, and merging two markers would produce a context
	neither file states — which is worse than the one the closer file asked for.
	"""

	here = (start or pathlib.Path.cwd()).resolve()

	for directory in (here, *here.parents):
		found = directory / FILE_NAME

		if found.is_file():
			return _read(found)

	return None


def _read (path: pathlib.Path) -> Marker | None:
	"""Parse one marker, treating anything unreadable as absent.

	**Unreadable is absent, not an error**, for the reason `context.read` gives: the whole
	design of this file is that losing it costs a question, so refusing to run because of a
	stray character in it would be a worse outcome than the one it protects against.
	"""

	try:
		with path.open("rb") as handle:
			data = tomllib.load(handle)

	except (OSError, tomllib.TOMLDecodeError):
		return None

	values = {
		name: value.strip()
		for name, value in data.items()
		if name in KEYS and isinstance(value, str) and value.strip()
	}

	if not values:
		return None

	return Marker(path=path, **values)


def write (
	directory: pathlib.Path,
	*,
	connection: str | None = None,
	workspace: str | None = None,
	project: str | None = None,
) -> pathlib.Path:
	"""Write a marker into ``directory``, and return where it went.

	Written out rather than dumped, so the file explains itself to whoever opens it next —
	which, unlike everything else this program writes, is somebody who did not run the command
	and may be reading it in a code review.
	"""

	path = directory / FILE_NAME
	lines = [
		"# Which Subroutine project the work in this directory belongs to.",
		"# Written by 'subroutine use --here'. Safe to delete: without it, new items go",
		"# wherever they went before, and nothing already recorded changes.",
	]

	for name, value in (
		("connection", connection), ("workspace", workspace), ("project", project)
	):
		if value:
			lines.append(f'{name} = "{value}"')

	path.write_text("\n".join(lines) + "\n", encoding="utf-8")

	return path
