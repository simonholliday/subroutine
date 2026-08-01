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

- **The project's id, with its key beside it** (`#177`). This began as a key alone, argued
  three ways: ``SR`` is readable, it is stable because §5.2 forbids renaming one, and a person
  can check it. The middle clause was the load-bearing one and `#176` removed it — a key can be
  renamed now, and the day somebody does, every checkout on every machine silently stops
  knowing where its work goes. So the **id** is the authority and the key stays beside it, which
  keeps the readability argument whole: this is an addition rather than a swap.
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
#: spells them on purpose; ``project`` and ``project_id`` are what this file adds, and the
#: reason it exists.
#:
#: **Both, not either.** The id is what survives a rename (`#177`); the key is what a person
#: reading this in a code review can recognise. Dropping the key would make the file a pair of
#: UUIDs nobody can check, and dropping the id would make it stale the first time somebody
#: renames a project.
KEYS = ("connection", "workspace", "project", "project_id")


class Marker(typing.NamedTuple):
	"""What a directory says it belongs to, and where that was written down."""

	path: pathlib.Path
	connection: str | None = None
	workspace: str | None = None
	project: str | None = None

	#: The project's permanent identifier. Preferred over ``project`` wherever both are
	#: present, because a key can be renamed and this cannot.
	project_id: str | None = None

	def describe (self) -> str:
		"""Return how a person reads this — ``SR, from ../.subroutine``."""

		named = "/".join(
			part for part in (self.connection, self.workspace, self.project) if part
		)

		return f"{named or 'nothing'}, from {self.path}"


class Named(typing.Protocol):
	"""The two fields a marker is resolved against, on whatever a client hands back."""

	@property
	def id (self) -> typing.Any:
		"""The project's permanent identifier."""

	@property
	def key (self) -> str:
		"""The project's current key."""


def resolve (marker: Marker, projects: typing.Iterable[Named]) -> str | None:
	"""Return the current key of the project a marker names, or ``None`` if there is none.

	By id where the marker carries one, because that is the half that survives a rename
	(`#177`); by key otherwise, which is every marker written before that change — including
	the one in this repository. A marker that predates it must go on working, or the upgrade
	is the outage.

	**Returning ``None`` is an answer, not a failure** (`#166`). A marker is advisory context
	written by a machine, so a checkout marked for one instance must not stop work being filed
	against another — the caller reports that it was ignored and carries on. This lives here,
	client-free and taking rows rather than fetching them, because both surfaces need the same
	answer and the CLI had the only copy: `subroutine_add` passed the marker's key straight to
	the server and refused whenever it did not exist there (`#228`'s neighbour, `#232`).
	"""

	if marker.project_id is not None:
		for row in projects:
			if str(row.id) == marker.project_id:
				return row.key

	if marker.project is not None:
		for row in projects:
			if row.key.upper() == marker.project.upper():
				return row.key

	return None


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
	project_id: str | None = None,
) -> pathlib.Path:
	"""Write a marker into ``directory``, and return where it went.

	Written out rather than dumped, so the file explains itself to whoever opens it next —
	which, unlike everything else this program writes, is somebody who did not run the command
	and may be reading it in a code review.

	The id carries the key as a trailing comment, so the file stays readable to a person while
	being durable to a rename (`#177`). A bare UUID with nothing beside it is the thing this
	file's own docstring once argued against, and it was right about that part.
	"""

	path = directory / FILE_NAME
	lines = [
		"# Which Subroutine project the work in this directory belongs to.",
		"# Written by 'subroutine use --here'. Safe to delete: without it, new items go",
		"# wherever they went before, and nothing already recorded changes.",
	]

	for name, value in (("connection", connection), ("workspace", workspace)):
		if value:
			lines.append(f'{name} = "{value}"')

	if project_id:
		named = f"  # {project}" if project else ""

		lines.append(f'project_id = "{project_id}"{named}')

	if project:
		lines.append(f'project = "{project}"')

	path.write_text("\n".join(lines) + "\n", encoding="utf-8")

	return path
