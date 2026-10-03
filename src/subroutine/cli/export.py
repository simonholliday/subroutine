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
import subroutine.addressing
import subroutine.cli.personal
import subroutine.clients.base
import subroutine.domain.projects
import subroutine.errors
import subroutine.exporting
import subroutine.installations
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
	"workspace": ("workspace", "workspaces"),
	"members": ("member", "members"),
	"project_members": ("project membership", "project memberships"),
}


#: The workspace's own row and words rather than its work: written in full, and summed up when
#: said, because *1 workspace, 15 statuses, 13 types* tells somebody leaving nothing they asked
#: about.
WORDS = frozenset({"workspace", "statuses", "item_types", "link_types"})


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

		It reads one connection: the one a write would go to, or the one -c names, from an
		instance running 0.10.0 or later.

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

		# **Read as every command reads a workspace's name** (`#4317`, of the cold review of
		# 2026-10-03): compared letter for letter, ``-w METACORTEX`` found nothing where ``list``
		# found it, and so did an id written in capitals.
		chosen = [
			workspace
			for workspace in reached.identity.workspaces
			if asked is None
			or subroutine.addressing.names_workspace(asked, slug=workspace.slug, identifier=workspace.id)
		]

		if not chosen:
			program.fail(
				subroutine.errors.NotFound(
					f"{reached.name} has no workspace called {asked!r} that you can read.",
					hint="'subroutine whoami' names the ones you can.",
				)
			)

		for workspace in chosen:
			folder: pathlib.Path | None = None

			try:
				folder = folder_for(directory, workspace, connection=reached.name)
				written = write(reached.client, folder, workspace=workspace, connection=reached.name)

			except subroutine.errors.SubroutineError as error:
				program.fail(error)

			# **What cannot be written is the reader's to put right, never a crash report** (`#4283`, M5
			# of the cold review of 2026-10-03): a name too long for the filesystem, a file where the
			# folder should be, or a folder this account may not write in each ended in *please report
			# it*, with the files written so far left behind and no word of what they were.
			except OSError as error:
				program.fail(_not_written(directory if folder is None else folder, error))

			for said in described(workspace, written):
				program.say(said)


def _not_written (folder: pathlib.Path, error: OSError) -> subroutine.errors.SubroutineError:
	"""Return the refusal for an export that could not be written, and what it left behind.

	**Where the export got to is said, not only that it stopped**: a folder holding what was
	written and no ``manifest.json`` is refused by the next export as not empty, so the reader is
	told it is an unfinished export and theirs to delete.
	"""

	where = error.filename or folder
	unfinished = folder.is_dir() and not (folder / MANIFEST).exists() and any(folder.iterdir())

	return subroutine.errors.ServiceUnavailable(
		f"The export could not be written to {where}: {error.strerror or error}.",
		hint=(
			f"{folder} holds what was written before it stopped, and no {MANIFEST}, so it is an "
			"unfinished export and can be deleted. "
			if unfinished
			else ""
		)
		+ "Name a folder this account can write to, on a disk with room.",
	)


def folder_for (
	directory: pathlib.Path, workspace: subroutine.views.WorkspaceRef, *, connection: str
) -> pathlib.Path:
	"""Return the folder one workspace's export goes in, refusing a name no instance makes.

	**The workspace's short name comes from the server**, and an export is as often taken from
	somebody else's instance as from one's own - by somebody leaving it, from the one party a
	hostile name would come from. A short name is stored in the form
	:func:`subroutine.addressing.normalize_slug` gives it, letters, digits and hyphens, so one that
	is not already in that form was not sent by an instance behaving as one. The export stops
	rather than tidying it, because a tidied name would hide that anything was wrong.
	"""

	slug = workspace.slug

	if not slug or subroutine.addressing.normalize_slug(slug) != slug:
		raise _not_one_an_instance_makes(connection, "a workspace name", slug)

	return _inside(directory, directory / slug, connection=connection, named=slug)


def _inside (
	folder: pathlib.Path, place: pathlib.Path, *, connection: str, named: str
) -> pathlib.Path:
	"""Return ``place``, refusing it unless it is ``folder`` or somewhere beneath it.

	The backstop behind the checks on each name: whatever a name holds, nothing an export writes
	may land outside the folder it was asked to write into.
	"""

	if not place.resolve().is_relative_to(folder.resolve()):
		raise _not_one_an_instance_makes(connection, "a name", named)

	return place


def _not_one_an_instance_makes (
	connection: str, what: str, named: str
) -> subroutine.errors.SubroutineError:
	"""Return the refusal for a name from the server that an export will not write under."""

	return subroutine.errors.ServiceUnavailable(
		f"{connection} sent {what} no instance makes, {named!r}, so the export stopped rather "
		"than write where it points.",
		hint="Check that this connection's address is the instance you meant, and that it is "
		"reached over https.",
	)


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
		# **An export that stopped part way is recognised by name** (`#4283`): files of lines and
		# no manifest are what one leaves, and the reader asking again needs telling it is theirs.
		stopped = (
			folder.is_dir()
			and not (folder / MANIFEST).exists()
			and any(folder.glob("*.jsonl"))
		)

		raise subroutine.errors.Conflict(
			f"{folder} already holds something, and an export writes only into an empty folder.",
			hint=(
				f"It has no {MANIFEST}, so it is an export that did not finish, and can be deleted. "
				"Or name a folder that does not exist yet."
				if stopped
				else "Name a folder that does not exist yet, or empty this one first."
			),
		)

	folder.mkdir(parents=True, exist_ok=True)
	counts: dict[str, int] = {}
	refused: dict[str, str] = {}

	# **Kept as they are written, for the readable copy, rather than read back afterwards**
	# (`#4054`): on a network share a file written and then read in one process can hang, and a
	# share is an ordinary place to want an export. **The cost is memory** (`#4317`): every item,
	# document and comment of the workspace is held until the pages are written, about 11 KB a
	# task by the count of the cold review of 2026-10-03.
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

			# **And none of its pages** (`#4317`): refused part way, the rows already read still
			# became pages, beside a manifest saying the kind was refused.
			if kind in held:
				held[kind].clear()

		# **An instance older than export answers that nothing is there** (`#4317`), and left an
		# empty file behind, so the next export into the same place was refused as not empty.
		except subroutine.errors.NotFound as error:
			target.unlink(missing_ok=True)

			raise _older_than_export(client, error) from error

	pages = _markdown(folder / "markdown", held, connection=connection)
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

	(folder / MANIFEST).write_text(
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

#: The release ``/v1/export/*`` first ships in. An instance older than it has no such route.
EXPORT_SINCE = (0, 10, 0)


def _older_than_export (
	client: subroutine.clients.base.Client, error: subroutine.errors.NotFound
) -> subroutine.errors.SubroutineError:
	"""Return why an instance answered ``not_found`` to an export, where it is too old - `#4317`.

	A path with no route and an item that does not exist both answer ``not_found``, so the
	instance's own version decides, and where it cannot be ranked - a build from source - the
	answer is left as the instance gave it.
	"""

	try:
		running = client.me().instance_version

	except subroutine.errors.SubroutineError:
		return error

	ranked = None if running is None else subroutine.installations.ordered(running)

	if ranked is None or ranked >= EXPORT_SINCE:
		return error

	return subroutine.errors.NotFound(
		f"This instance runs Subroutine {running}, and export needs "
		f"{'.'.join(str(part) for part in EXPORT_SINCE)} or later.",
		hint="Whoever runs it can upgrade it, and an export then takes the same command.",
	)

#: The file an export writes last, so a folder without one is an export that did not finish.
MANIFEST = "manifest.json"

#: The most a page's file name may take, in bytes (`#4283`, M5 of the cold review of 2026-10-03).
#:
#: **The tightest of the common filesystems**: eCryptfs, an encrypted home folder, takes 143 bytes
#: where ext4 and most others take 255. Eighty characters of a title fit every one of them in a
#: script written a byte a character, and eighty emoji are 320 bytes, which none of them takes.
NAME_BYTES = 143

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


def _markdown (
	folder: pathlib.Path, held: dict[str, list[typing.Any]], *, connection: str
) -> int:
	"""Write a page for each item and document, foldered by project, and return how many.

	A deleted one goes under :data:`TRASH`. Comments follow the page they were made on, oldest
	first, as the comment listing reads them.

	**Every page is placed before any is written**, so a project path no instance makes stops the
	export with no page written under any project, rather than part way through.
	"""

	said: dict[uuid.UUID, list[typing.Any]] = {}

	for comment in held.get("comments", []):
		said.setdefault(comment.entity_id, []).append(comment)

	placed = [
		(
			kind,
			item,
			folder / TRASH
			if item.deleted_at is not None
			else _project_folder(folder, item.project_path, connection=connection),
		)
		for kind in FRONT
		for item in held.get(kind, [])
	]

	for kind, item, place in placed:
		place.mkdir(parents=True, exist_ok=True)
		(place / filename(item.ref, item.title)).write_text(
			page(kind, item, said.get(item.id, [])), encoding="utf-8"
		)

	return len(placed)


def _project_folder (folder: pathlib.Path, path: str, *, connection: str) -> pathlib.Path:
	"""Return where one project's pages go, refusing a project path no instance makes.

	A project path is project keys joined by
	:data:`~subroutine.domain.projects.PATH_SEPARATOR`, and a key matches
	:data:`~subroutine.domain.projects.KEY_PATTERN`, which holds no dot and no separator. So a
	path whose every segment is a key cannot climb out of the folder or name a place outside it,
	and one that is not was not sent by an instance behaving as one.
	"""

	keys = (path or "").split(subroutine.domain.projects.PATH_SEPARATOR)

	if not all(subroutine.domain.projects.KEY_PATTERN.fullmatch(key) for key in keys):
		raise _not_one_an_instance_makes(connection, "a project path", path)

	return _inside(folder, folder.joinpath(*keys), connection=connection, named=path)


def filename (ref: int, title: str) -> str:
	"""Return a page's file name: its number, then its title made safe to name a file with.

	**The number first**, so two items with one title are two files, and a listing of the folder
	reads in the order the items were made.
	"""

	safe = " ".join(_UNSAFE.sub(" ", title).split())[:80].rstrip(" .")

	# **And no more bytes than a filesystem takes** (`#4283`). The eighty characters stay, so a
	# title written a byte a character is cut where it always was; a cut inside a character's
	# bytes drops that character rather than leaving half of it.
	room = NAME_BYTES - len(f"{ref} .md".encode())
	safe = safe.encode()[:room].decode(errors="ignore").rstrip(" .")

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
		"  "
		+ (", ".join(held) or "No items")
		+ ", and the workspace's settings and the words it uses for states and types.",
	]

	for kind, reason in written.refused.items():
		lines.append(f"  No {NOUNS[kind][1]}: {reason}")

	if written.pages:
		lines.append("  A readable copy is in markdown/, a page for each item with its comments.")

	lines.append("  manifest.json says what is in it, and what an export never holds.")

	return lines
