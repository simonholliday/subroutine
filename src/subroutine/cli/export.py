"""``subroutine export``: take away everything you can read, as files - `#4053`, decision `#4049`.

**A module of its own rather than one more command in ``cli/personal.py``'s ``register``**,
whose length only goes down (`#943`). It is registered beside that one, with the same
:class:`~subroutine.cli.personal.Program`, so it speaks and reaches a connection the same way.

**It reads through a client and never opens a database itself**, because the people an export is
for are as often on somebody else's instance as on their own: ``Client.export`` pages through
``GET /v1/export/<kind>`` or through the same function locally.
"""

import dataclasses
import datetime
import json
import pathlib

import typer

import subroutine
import subroutine.cli.personal
import subroutine.clients.base
import subroutine.errors
import subroutine.exporting
import subroutine.views

#: What a kind is called in a sentence, one and many.
NOUNS: dict[str, tuple[str, str]] = {
	"projects": ("project", "projects"),
	"tasks": ("item", "items"),
	"documents": ("document", "documents"),
	"comments": ("comment", "comments"),
	"links": ("link", "links"),
	"verifications": ("check", "checks"),
	"events": ("change", "changes"),
	"tags": ("tag", "tags"),
	"statuses": ("status", "statuses"),
	"item_types": ("type", "types"),
	"link_types": ("kind of link", "kinds of link"),
	"saved_views": ("saved view", "saved views"),
	"users": ("account", "accounts"),
}


#: The workspace's own words rather than its work: written in full, and summed up when said,
#: because *15 statuses, 13 types* tells somebody leaving nothing they asked about.
WORDS = frozenset({"statuses", "item_types", "link_types"})


@dataclasses.dataclass(frozen=True)
class Written:
	"""What one workspace's export holds, and what it was refused."""

	folder: pathlib.Path
	counts: dict[str, int]
	refused: dict[str, str]


def register (app: typer.Typer, program: subroutine.cli.personal.Program) -> None:
	"""Add ``export`` to the program."""

	@app.command()
	def export (
		directory: pathlib.Path = typer.Argument(
			..., help="Where to write it. Each workspace becomes a folder inside."
		),
	) -> None:
		"""Take away everything you can read, as files you can keep.

		Each workspace becomes a folder holding one file for each kind of thing - items,
		documents, comments, links, changes, tags and the rest - with one line per row, and a
		manifest saying what is in it, what was left out, and which version wrote it. Done,
		archived and deleted items are included.

		Nothing you could not already read is in it, and no password, token or sign-in link
		ever is. Name a workspace with -w to take only that one.

		Examples:

		  subroutine export ~/leaving

		  subroutine -c work -w metacortex export ~/leaving
		"""

		_exported(program, directory=directory)


def _exported (program: subroutine.cli.personal.Program, *, directory: pathlib.Path) -> None:
	"""Write a folder for each workspace asked for, and say what went into each."""

	with program.opened() as world:
		try:
			reached = world.writing_to()

		except subroutine.errors.SubroutineError as error:
			program.fail(error)

		asked = program.selected.workspace
		chosen = [
			workspace
			for workspace in reached.identity.workspaces
			if asked is None or asked in (workspace.slug, str(workspace.id))
		]

		if not chosen:
			program.fail(
				subroutine.errors.NotFound(
					f"{reached.name} has no workspace called {asked!r} that you can read.",
					hint="'subroutine whoami' names the ones you can.",
				)
			)

		for workspace in chosen:
			try:
				written = write(
					reached.client,
					directory / workspace.slug,
					workspace=workspace,
					connection=reached.name,
				)

			except subroutine.errors.SubroutineError as error:
				program.fail(error)

			for said in described(workspace, written):
				program.say(said)


def write (
	client: subroutine.clients.base.Client,
	folder: pathlib.Path,
	*,
	workspace: subroutine.views.WorkspaceRef,
	connection: str,
	now: datetime.datetime | None = None,
) -> Written:
	"""Write one workspace's export into an empty folder, the manifest last.

	**The manifest is written last, so a folder without one is an export that did not finish.**
	A kind the credential may not read is left out and named in the manifest as refused; any
	other failure stops the export there, and says so, rather than leaving a folder that
	looks whole.
	"""

	if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
		raise subroutine.errors.Conflict(
			f"{folder} already holds something, and an export writes only into an empty folder.",
			hint="Name a folder that does not exist yet, or empty this one first.",
		)

	folder.mkdir(parents=True, exist_ok=True)
	counts: dict[str, int] = {}
	refused: dict[str, str] = {}

	for kind in subroutine.views.EXPORTED:
		target = folder / f"{kind}.jsonl"

		try:
			counts[kind] = _lines(client, kind, target, workspace=workspace.slug)

		except subroutine.errors.Forbidden as error:
			target.unlink(missing_ok=True)
			refused[kind] = error.detail

	me = client.me()
	manifest = {
		"format": "Subroutine export",
		"version": me.instance_version,
		"versioning": subroutine.exporting.VERSIONING,
		"schema_revision": me.schema_revision,
		"schema_revision_is": (
			"Which migration the database was at. Information only: the version above is the "
			"format."
		),
		"written_with": f"subroutine {subroutine.__version__}",
		"exported_at": (now or datetime.datetime.now(datetime.UTC)).isoformat(),
		"connection": connection,
		"as": me.user.username,
		"workspace": {"id": str(workspace.id), "slug": workspace.slug, "title": workspace.title},
		"files": {f"{kind}.jsonl": count for kind, count in counts.items()},
		"refused": {f"{kind}.jsonl": reason for kind, reason in refused.items()},
		"never_in_it": list(subroutine.exporting.NEVER_IN_IT),
		"fields_left_out": {
			kind: sorted(fields) for kind, fields in subroutine.exporting.COMPUTED.items() if fields
		},
	}

	(folder / "manifest.json").write_text(
		json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
	)

	return Written(folder=folder, counts=counts, refused=refused)


def _lines (
	client: subroutine.clients.base.Client, kind: str, target: pathlib.Path, *, workspace: str
) -> int:
	"""Write every row of one kind as a line of JSON, and return how many there were."""

	count = 0

	with target.open("w", encoding="utf-8", newline="\n") as out:
		for item in client.export(kind, workspace=workspace):
			out.write(json.dumps(subroutine.exporting.line(kind, item), ensure_ascii=False))
			out.write("\n")
			count += 1

	return count


def described (workspace: subroutine.views.WorkspaceRef, written: Written) -> list[str]:
	"""Return what to say about one workspace's export: where, how much, and what was refused."""

	held = [
		f"{count:,} {NOUNS[kind][0] if count == 1 else NOUNS[kind][1]}"
		for kind, count in written.counts.items()
		if count and kind not in WORDS
	]
	lines = [
		f"Exported {workspace.slug} to {written.folder}:",
		"  " + (", ".join(held) or "No items") + ", and the words it uses for states and types.",
	]

	for kind, reason in written.refused.items():
		lines.append(f"  No {NOUNS[kind][1]}: {reason}")

	lines.append("  manifest.json says what is in it, and what an export never holds.")

	return lines
