"""Which connection and which workspace a bare number means — SPEC.md §13.7.

Refs are per-workspace integers (§6.2), so the full namespace is ``connection/workspace/ref``
and *every* low number exists in most workspaces. That makes ambiguity the normal case rather
than an exotic one, and this module is what keeps it from costing anything to type.

**The current context is a (connection, workspace) pair, and it is switchable.** Not a
property of the connection: a connection is an instance plus a credential, and its reach is
whatever the credential grants (§7.4). Binding a workspace into the connection definition
would buy shorter addresses by removing access the token legitimately gives. This is
``kubectl``'s split — cluster and user are the connection, namespace is *selected*, the
context is the pair — and it is right for the same reason.

Resolution order, extending the server's own (``requested → pinned → sole → refuse``):

1. ``--connection`` / ``--workspace`` on the command
2. ``SUBROUTINE_CONNECTION`` / ``SUBROUTINE_WORKSPACE`` in the environment, so a shell or a
   terminal pane can pin itself
3. a ``.subroutine`` marker at or above the working directory (§13.7a, ``subroutine.directory``)
4. the stored context, set by ``subroutine use``
5. the connection's sole workspace, when its credential reaches exactly one
6. otherwise **refuse, naming the candidates**

Steps 1 to 4 are answered here. **Step 3 is above the stored context and below the
environment**, which is the only ordering that works: a marker describes *this checkout* and so
must beat a machine-global `use`, while a flag or an exported variable is somebody saying
something now and must beat a file they may have forgotten about. Steps 4 and 5 need a connection to have been asked what it
reaches, so they belong to whoever holds a client.

**``use`` changes what a bare number means. It never changes what you can see.** That is the
load-bearing rule: reads span everything reachable — §13.7 exists for the questions that cross
the boundary, so a context that hid the dentist appointment would defeat it — and writes target
the current context only. Because nothing is ever hidden, forgetting your context cannot cause
you to miss something, which is what makes it usable without a banner on every response.

**The stored file is safe to lose, and that is a property worth stating.** It holds only which
workspace is current, and every ref stays absolute within one, so losing it makes ``subroutine
done 42`` *ask* which 42. It is emphatically not the file deleted in §12.2a, which held a
mapping from numbers to items and whose loss silently changed what an identifier meant. The
test: losing this state must degrade to a question, never to a different outcome.
"""

import contextlib
import dataclasses
import os
import pathlib
import tomllib
import typing

import subroutine.config
import subroutine.connections
import subroutine.directory
import subroutine.domain.workspaces
import subroutine.errors

#: Where the stored context lives. Under ``STATE_HOME`` rather than the data directory: XDG
#: describes that as state which should persist between restarts but is not important enough
#: for the data directory, which is exactly this.
FILE_NAME = "context.toml"

CONNECTION_VARIABLE = "SUBROUTINE_CONNECTION"
WORKSPACE_VARIABLE = "SUBROUTINE_WORKSPACE"

#: How each half of the context was arrived at. Provenance is the part that earns its keep:
#: the standing footgun in comparable tooling is not having a profile but not knowing whether
#: it came from a flag, the environment or a file.
FROM_FLAG = "the command line"
FROM_DIRECTORY = "the .subroutine file here"
FROM_STORED = "'subroutine use'"
FROM_DEFAULT = "the default connection"
FROM_SOLE = "the only one there is"
FROM_NOTHING = "nothing"

#: The sources that will answer the *next* command the way they answered this one. A flag
#: settles one invocation and nothing after it, and an environment variable may have been a
#: one-shot prefix — indistinguishable here from an exported one, so it is counted as not
#: carrying. Erring that way costs a longer address in printed advice; erring the other way
#: prints a command that acts somewhere else (`#280`).
CARRIED = frozenset(
	{FROM_DIRECTORY, FROM_STORED, FROM_DEFAULT, FROM_SOLE, FROM_NOTHING}
)


@dataclasses.dataclass(frozen=True)
class Current:
	"""The connection and workspace a bare number means, and where each came from."""

	connection: str
	connection_source: str

	#: What a ``.subroutine`` marker asked for and could not have — item `#409`. Carried back
	#: rather than warned about here, because this module has no output and the CLI already
	#: owns the sentence its workspace twin prints. ``None`` is the ordinary case.
	unusable_marker_connection: str | None = None

	#: The connection a marker's workspace was *found* on when the name it gave is not
	#: configured here — item `#556`, building `#330`'s decision. Set only by the CLI, which
	#: is the layer that has asked the connections anything; this module resolves before any
	#: of them has been reached and cannot know.
	#:
	#: **It says the marker speaks for that connection after all**, which is what lets the
	#: project half be honoured too. `#414` is why that is a separate field rather than a
	#: quiet reassignment: a marker whose connection was dropped must not match its project by
	#: *key* somewhere else, and only an id match earns this.
	marker_found_on: str | None = None

	#: ``None`` when nothing has said which workspace, which is not yet a problem: a
	#: connection reaching exactly one workspace answers it, and only a connection reaching
	#: several turns it into a refusal.
	workspace: str | None = None
	workspace_source: str = FROM_NOTHING

	def with_workspace (self, slug: str, source: str) -> "Current":
		"""Return this context with the workspace settled by a later step."""

		return dataclasses.replace(self, workspace=slug, workspace_source=source)

	@property
	def persists (self) -> bool:
		"""Report whether the next bare command will resolve to the same place.

		The question printed advice has to ask. A tip naming an item, or a footer saying
		what a bare number means, is read *after* this command has finished — so it is a
		statement about the next one, and a ``-c``/``-w`` flag has by then expired.
		"""

		return {self.connection_source, self.workspace_source} <= CARRIED

	def describe (self, *, qualified: bool) -> str:
		"""Return the context as a person reads it — ``work/acme (from …)``.

		``qualified`` is false with a single connection, where naming it would be noise: there
		is nothing to disambiguate and §13.5b's output has no room for a word nobody needs.
		"""

		where = self.workspace or "(not chosen yet)"
		address = f"{self.connection}/{where}" if qualified else where

		if self.workspace_source == self.connection_source:
			return f"{address} (from {self.workspace_source})"

		if not qualified:
			return f"{address} (from {self.workspace_source})"

		return (
			f"{address} (connection from {self.connection_source}, "
			f"workspace from {self.workspace_source})"
		)


def file_path () -> pathlib.Path:
	"""Return where the stored context lives, whether or not it exists."""

	return subroutine.config.state_home() / FILE_NAME


def resolve (
	roster: subroutine.connections.Roster,
	*,
	connection: str | None = None,
	workspace: str | None = None,
	marker: subroutine.directory.Marker | None = None,
) -> Current:
	"""Answer steps 1 to 3 of §13.7's resolution order.

	The two halves are resolved *independently*, which is deliberate: ``-w acme`` on a command
	should not throw away the connection somebody chose with ``use``, and exporting
	``SUBROUTINE_WORKSPACE`` in one terminal should not silently move which instance writes
	land on.

	Anything the flags or the environment name is checked against the roster here, so a typo
	is a refusal listing what exists rather than a request sent nowhere.
	"""

	stored = read()
	candidates = (
		(connection, FROM_FLAG),
		(os.environ.get(CONNECTION_VARIABLE), CONNECTION_VARIABLE),
		(None if marker is None else marker.connection, FROM_DIRECTORY),
		(stored.get("connection"), FROM_STORED),
	)
	chosen, source = _first(*candidates)
	unusable = None

	# **A marker naming a connection that is not there is dropped, not obeyed** (`#409`).
	# `#166`'s rule is that the one thing a marker must not do is break the program, and it
	# was implemented for the *workspace* half only: `cli/personal._settled` runs after the
	# connections have been asked, and this runs before, so `require` below raised and nothing
	# downstream ever got the chance to be lenient.
	#
	# It broke *reading*, which makes it worse than `#324`. §13.7's load-bearing rule is that
	# reads span everything reachable — forgetting your context must never cost you something
	# — and a `subroutine list` that cannot run at all is the strongest possible version of
	# that failure. Two ordinary ways in: a connection renamed in `config.toml`, and one set
	# `enabled = false`, which `roster` drops so it is indistinguishable from a missing one.
	#
	# **Only the marker.** A flag or an environment variable is somebody speaking now and
	# still refuses loudly; the stored context is refused too, because `use` is a thing
	# somebody typed and a name that has since gone is worth being told about. The same line
	# `_settled` draws, one field over.
	if chosen is not None and source == FROM_DIRECTORY and roster.find(chosen) is None:
		unusable = chosen
		chosen, source = _first(
			*(pair for pair in candidates if pair[1] != FROM_DIRECTORY)
		)

	if chosen is None:
		chosen, source = roster.default, (
			FROM_SOLE if len(roster) == 1 else FROM_DEFAULT
		)

	# Refuses here rather than at the first request, and names what exists. A stored context
	# pointing at a connection that has since been removed is the common way to arrive here,
	# and "there is no connection called 'work'" is a great deal more useful than a timeout.
	roster.require(chosen)

	wanted, workspace_source = _first(
		(workspace, FROM_FLAG),
		(os.environ.get(WORKSPACE_VARIABLE), WORKSPACE_VARIABLE),
		# Only when the marker also chose the connection, or did not name one — the same rule
		# the stored context follows below, and for the same reason: a workspace slug means
		# nothing on an instance that has never heard of it.
		(
			None
			if marker is None or (marker.connection or chosen) != chosen
			else marker.workspace,
			FROM_DIRECTORY,
		),
		# The stored workspace only applies to the connection it was stored *with*. Carrying
		# it across would say `acme` on an instance that has never heard of it.
		(stored.get("workspace") if stored.get("connection") == chosen else None, FROM_STORED),
	)

	# **Both halves are normalised, and not doing so was a §13.5b regression.** `Roster.find`
	# and `Identity.workspace` both match case-insensitively, so `use PERSONAL` and `-c LOCAL`
	# *resolved* — and then `World.address_of` compared the stored text against the canonical
	# slug, failed, and qualified every address on a one-workspace installation. A
	# capitalisation turned on labelling where there was nothing to disambiguate.
	return Current(
		connection=roster.require(chosen).name,
		connection_source=source,
		unusable_marker_connection=unusable,
		workspace=None if wanted is None else subroutine.domain.workspaces.normalize_slug(wanted),
		workspace_source=workspace_source if wanted is not None else FROM_NOTHING,
	)


def stored_workspace (connection: str) -> str | None:
	"""Return the workspace ``subroutine use`` stored for this connection, if any.

	**The step after the marker in §13.7's order, callable on its own** — item ``#324``. A
	marker that names somewhere the connection has never heard of is dropped, and what should
	follow is the *next* source rather than nothing: dropping to nothing turned a stale marker
	from advisory context into something that broke commands which would otherwise have
	worked, which is precisely what `#166` says a marker must never do.

	Narrowed to the connection it was stored with, exactly as :func:`resolve` narrows it, and
	for the same reason: a slug means nothing on an instance that has never heard of it. Two
	copies of that condition would be one more place for the chain to disagree with itself,
	so this is the function ``resolve`` should eventually read too.

	**Normalised, which it was not** (`#418`). ``resolve`` puts both halves through
	``workspaces.normalize_slug`` and says why three lines above: not doing so was a §13.5b
	regression, where a capitalisation turned on labelling that had nothing to disambiguate.
	This was written beside that comment and omitted it — and ``use`` stores what was typed, so
	``subroutine use PROJECTS`` left the caller comparing ``'PROJECTS'`` against the canonical
	``'projects'``. `#324`'s fallback then silently did not apply, degrading to the *old*
	behaviour with the *old* message, which is the hardest kind of failure to notice.
	"""

	stored = read()

	if stored.get("connection") != connection:
		return None

	wanted = stored.get("workspace")

	if wanted is None or not wanted.strip():
		return None

	return subroutine.domain.workspaces.normalize_slug(wanted.strip())


def _first (*candidates: tuple[str | None, str]) -> tuple[str | None, str]:
	"""Return the first candidate that has a value, with where it came from."""

	for value, source in candidates:
		if value is not None and value.strip():
			return value.strip(), source

	return None, FROM_NOTHING


def read () -> dict[str, str]:
	"""Return the stored context, or an empty mapping when there is none.

	**A file that cannot be read is treated as absent, not as an error.** This is the one
	place in the program where that is right: the whole design of this file is that losing it
	costs a question, so refusing to run because of it would be a worse outcome than the one
	it is protecting against.
	"""

	path = file_path()

	if not path.is_file():
		return {}

	try:
		with path.open("rb") as handle:
			data = tomllib.load(handle)

	except (OSError, tomllib.TOMLDecodeError):
		return {}

	return {
		name: value
		for name, value in data.items()
		if name in {"connection", "workspace"} and isinstance(value, str)
	}


def store (connection: str, workspace: str | None) -> pathlib.Path:
	"""Record the current context, and return where it was written."""

	path = file_path()
	path.parent.mkdir(parents=True, exist_ok=True)

	lines = [
		"# Which connection and workspace a bare number means.",
		"# Written by 'subroutine use'; safe to delete, and 'subroutine use --reset' does.",
		f'connection = "{connection}"',
	]

	if workspace is not None:
		lines.append(f'workspace = "{workspace}"')

	path.write_text("\n".join(lines) + "\n", encoding="utf-8")

	return path


def clear () -> pathlib.Path | None:
	"""Forget the stored context, returning what was removed or ``None``.

	What is left is the configured default, which is what ``use --reset`` means: not "no
	context" — there is always one — but "stop overriding the one the configuration chose".
	"""

	path = file_path()

	if not path.is_file():
		return None

	with contextlib.suppress(OSError):
		path.unlink()

	return path


def refuse (
	roster: subroutine.connections.Roster,
	current: Current,
	candidates: typing.Sequence[str],
) -> typing.NoReturn:
	"""Step 5: refuse an unsettled workspace, naming the ones there are.

	Ambiguity is a refusal and never a guess. Until 2026-07-29 the CLI resolved a bare ref
	with ``.first()`` on an unordered query across every readable workspace, so two workspaces
	each holding a ``#1`` was enough for ``subroutine done 1`` to complete whichever row the
	database happened to return — and no test could see it, because every fixture had exactly
	one workspace.
	"""

	listed = ", ".join(candidates)
	where = current.connection if roster.qualifies else ""
	example = f"{where}/{candidates[0]}" if where else candidates[0]

	raise subroutine.errors.ValidationError(
		f"{current.connection} has several workspaces, so there is no way to tell which one "
		"this is about.",
		code="missing_field",
		errors=[
			subroutine.errors.FieldError(
				field="workspace",
				code="missing_field",
				message=f"Workspaces here: {listed}.",
			)
		],
		hint=f"Say which — 'subroutine -w {candidates[0]} …' for one command, or "
		f"'subroutine use {example}' to keep working there.",
	)
