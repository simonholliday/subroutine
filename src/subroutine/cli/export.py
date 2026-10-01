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
import re
import typing
import uuid

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

	#: How many files the readable copy in ``markdown/`` holds.
	pages: int = 0


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

	# **Kept as they are written, for the readable copy, rather than read back afterwards**
	# (`#4054`): on a network share a file written and then read in one process can hang, and a
	# share is an ordinary place to want an export.
	held: dict[str, list[typing.Any]] = {kind: [] for kind in READ_AS_PAGES}

	for kind in subroutine.views.EXPORTED:
		target = folder / f"{kind}.jsonl"

		try:
			counts[kind] = _lines(
				client, kind, target, workspace=workspace.slug, keep=held.get(kind)
			)

		except subroutine.errors.Forbidden as error:
			target.unlink(missing_ok=True)
			refused[kind] = error.detail

	pages = _markdown(folder / "markdown", held)
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
		"markdown": {
			"files": pages,
			"is": (
				"A readable copy of the items and documents, one file each with its comments, made "
				"from the same rows. Lossy on purpose: the files of lines are the export."
			),
		},
		"never_in_it": list(subroutine.exporting.NEVER_IN_IT),
		"fields_left_out": {
			kind: sorted(fields) for kind, fields in subroutine.exporting.COMPUTED.items() if fields
		},
	}

	(folder / "manifest.json").write_text(
		json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
	)

	return Written(folder=folder, counts=counts, refused=refused, pages=pages)


def _lines (
	client: subroutine.clients.base.Client,
	kind: str,
	target: pathlib.Path,
	*,
	workspace: str,
	keep: list[typing.Any] | None = None,
) -> int:
	"""Write every row of one kind as a line of JSON, and return how many there were.

	``keep`` gathers the rows too, for a kind the readable copy is made from.
	"""

	count = 0

	with target.open("w", encoding="utf-8", newline="\n") as out:
		for item in client.export(kind, workspace=workspace):
			out.write(json.dumps(subroutine.exporting.line(kind, item), ensure_ascii=False))
			out.write("\n")
			count += 1

			if keep is not None:
				keep.append(item)

	return count


#: The kinds the readable copy is made from: the pages, and what was said on them.
READ_AS_PAGES = ("tasks", "documents", "comments")

#: Where a deleted item's page goes. **No project key can be it**, since a key has no underscore,
#: so the trash never shares a folder with a project called ``trash``.
TRASH = "_trash"

#: What each kind's front matter holds, under the name a person reads, in the order they read
#: it. The rest of a row is in its file of lines.
FRONT: dict[str, tuple[tuple[str, str], ...]] = {
	"tasks": (
		("ref", "ref"),
		("title", "title"),
		("type", "type"),
		("status", "status"),
		("project", "project_path"),
		("parent", "parent_ref"),
		("assignee", "assignee"),
		("importance", "importance"),
		("urgency", "urgency"),
		("due", "due_at"),
		("starts", "starts_at"),
		("ends", "ends_at"),
		("repeats", "recurrence_text"),
		("tags", "tags"),
		("created", "created_at"),
		("updated", "updated_at"),
		("done", "completed_at"),
		("deleted", "deleted_at"),
		("id", "id"),
	),
	"documents": (
		("ref", "ref"),
		("title", "title"),
		("type", "type"),
		("status", "status"),
		("project", "project_path"),
		("parent", "parent_ref"),
		("tags", "tags"),
		("created", "created_at"),
		("updated", "updated_at"),
		("deleted", "deleted_at"),
		("id", "id"),
	),
}

#: What no file name may hold on the systems people keep files on, and control characters.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')


def _markdown (folder: pathlib.Path, held: dict[str, list[typing.Any]]) -> int:
	"""Write a page for each item and document, foldered by project, and return how many.

	A deleted one goes under :data:`TRASH`. Comments follow the page they were made on, oldest
	first, as the comment listing reads them.
	"""

	said: dict[uuid.UUID, list[typing.Any]] = {}

	for comment in held.get("comments", []):
		said.setdefault(comment.entity_id, []).append(comment)

	pages = 0

	for kind in FRONT:
		for item in held.get(kind, []):
			place = folder / (TRASH if item.deleted_at is not None else item.project_path)
			place.mkdir(parents=True, exist_ok=True)
			(place / filename(item.ref, item.title)).write_text(
				page(kind, item, said.get(item.id, [])), encoding="utf-8"
			)
			pages += 1

	return pages


def filename (ref: int, title: str) -> str:
	"""Return a page's file name: its number, then its title made safe to name a file with.

	**The number first**, so two items with one title are two files, and a listing of the folder
	reads in the order the items were made.
	"""

	safe = " ".join(_UNSAFE.sub(" ", title).split())[:80].rstrip(" .")

	return f"{ref} {safe}.md" if safe else f"{ref}.md"


def page (kind: str, item: typing.Any, comments: typing.Sequence[typing.Any]) -> str:
	"""Return one item's page: front matter, its own text, then what was said on it.

	**Front matter as YAML whose every value is JSON** - a number, a quoted string, a list -
	which YAML 1.2 reads as written, so no YAML library is needed to write it or to read it.
	"""

	row = item.model_dump(mode="json")
	front = [
		f"{shown}: {json.dumps(row[field], ensure_ascii=False)}"
		for shown, field in FRONT[kind]
		if row.get(field) not in (None, [], "")
	]
	text = (item.description if kind == "tasks" else item.body) or ""
	parts = ["---", *front, "---", ""]

	if text.strip():
		parts += [text.rstrip(), ""]

	if comments:
		parts += ["## Comments", ""]

	for comment in comments:
		when = comment.created_at.strftime("%Y-%m-%d %H:%M UTC")
		parts += [f"**{comment.author or 'No account'}**, {when}", "", comment.body.rstrip(), ""]

	return "\n".join(parts)


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

	if written.pages:
		lines.append("  A readable copy is in markdown/, a page for each item with its comments.")

	lines.append("  manifest.json says what is in it, and what an export never holds.")

	return lines
