"""``subroutine export``: a folder per workspace, a file per kind, the manifest last - `#4053`.

Decision `#4049`. Driven through the real command against a fresh instance, as the personal
path's tests are, and through :func:`subroutine.cli.export.write` with a client that refuses one
kind, which a person's own instance never does.
"""

import datetime
import json
import os
import pathlib
import typing

import pytest
import typer.testing

import subroutine.cli.export
import subroutine.clients.base
import subroutine.domain.events
import subroutine.errors
import subroutine.exporting
import subroutine.views
import test_personal_path
import test_transport_equivalence

#: The personal path's fresh home and its runner for the real command line, bound here so
#: pytest finds them as this module's fixtures too.
home = test_personal_path.home
run = test_personal_path.run
pair = test_transport_equivalence.pair
Pair = test_transport_equivalence.Pair


@pytest.fixture(autouse=True)
def _every_event_settled (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Report events the moment they are written - ``test_export.py`` says why."""

	monkeypatch.setattr(subroutine.domain.events, "WATERMARK", datetime.timedelta(0))


def _lines (path: pathlib.Path) -> list[dict[str, typing.Any]]:
	"""Return a file of JSON lines as the rows it holds."""

	return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_an_export_is_a_folder_per_workspace_with_a_file_per_kind_and_a_manifest (
	run: typing.Callable[..., typer.testing.Result], tmp_path: pathlib.Path
) -> None:
	"""The whole of it, as somebody leaving would take it, and every count the manifest gives."""

	run("init")
	run("add", "Fix the deploy script")
	run("add", "Collect the package from reception")
	run("done", "2")

	said = " ".join(run("export", str(tmp_path / "leaving")).output.split())
	folder = next((tmp_path / "leaving").iterdir())
	manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))

	assert f"Exported {folder.name} to" in said and "2 items" in said, said
	assert {path.name for path in folder.iterdir()} == {
		"manifest.json",
		"markdown",
		*(f"{kind}.jsonl" for kind in subroutine.views.EXPORTED),
	}

	for name, count in manifest["files"].items():
		assert len(_lines(folder / name)) == count, name

	titles = {row["title"]: row for row in _lines(folder / "tasks.jsonl")}

	assert set(titles) == {"Fix the deploy script", "Collect the package from reception"}
	assert titles["Collect the package from reception"]["completed_at"], "what is done is in it"
	assert "rank" not in titles["Fix the deploy script"], "what is worked out is not"
	assert manifest["files"]["events.jsonl"] >= 3 and not manifest["refused"]
	assert manifest["version"] and manifest["schema_revision"] and manifest["as"]
	assert manifest["fields_left_out"]["tasks"] == sorted(
		subroutine.exporting.COMPUTED["tasks"]
	)
	assert manifest["markdown"]["files"] == 2 and "A readable copy is in markdown/" in said


def _front (text: str) -> dict[str, typing.Any]:
	"""Return a page's front matter, each value read as the JSON it was written as."""

	lines = text.split("\n")

	assert lines[0] == "---", text

	closing = lines.index("---", 1)

	return {
		key: json.loads(value)
		for key, value in (line.split(": ", 1) for line in lines[1:closing])
	}


def test_the_readable_copy_is_a_page_per_item_with_what_was_said_on_it (
	run: typing.Callable[..., typer.testing.Result], tmp_path: pathlib.Path
) -> None:
	"""`#4054`: foldered by project, the trash apart, and every value of the front matter JSON.

	JSON because YAML 1.2 reads it as written, so neither end needs a YAML library - which is
	the claim this reads back by parsing every value with ``json`` alone.
	"""

	run("init")
	run("add", "Fix the deploy script")
	run("add", "Order a new phone for reception")
	run("comment", "1", "Tried it on staging first.")
	run("delete", "2")
	run("export", str(tmp_path / "leaving"))

	pages = next((tmp_path / "leaving").iterdir()) / "markdown"
	(fixed,) = (pages / "inbox").glob("1 *.md")
	(gone,) = (pages / "_trash").glob("2 *.md")
	text = fixed.read_text(encoding="utf-8")
	front = _front(text)

	assert fixed.name == "1 Fix the deploy script.md"
	assert front["ref"] == 1 and front["title"] == "Fix the deploy script", front
	assert front["status"] == "open" and front["project"] == "inbox", front
	assert "## Comments" in text and "Tried it on staging first." in text, text
	assert _front(gone.read_text(encoding="utf-8"))["deleted"], "the trash says when"


@pytest.mark.parametrize(
	("title", "named"),
	[
		("Fix the deploy script", "12 Fix the deploy script.md"),
		('Plans: web/app "v2"?', "12 Plans web app v2.md"),
		("...", "12.md"),
		("x" * 200, f"12 {'x' * 80}.md"),
	],
)
def test_a_page_s_name_is_safe_on_every_common_system (title: str, named: str) -> None:
	"""No separator, no character Windows refuses, no trailing dot, and never too long."""

	assert subroutine.cli.export.filename(12, title) == named


def test_a_page_s_name_is_never_longer_than_a_filesystem_takes () -> None:
	"""`SR#4283`: eighty emoji are 320 bytes, and no common filesystem takes a name that long."""

	named = subroutine.cli.export.filename(12, "\N{GRINNING FACE}" * 80)

	assert len(named.encode()) <= subroutine.cli.export.NAME_BYTES, named
	assert named.startswith("12 \N{GRINNING FACE}") and named.endswith("\N{GRINNING FACE}.md"), named


@pytest.mark.parametrize("where", ["a file", "a long name"])
def test_an_export_that_cannot_be_written_says_so_rather_than_crashing (
	where: str,
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`SR#4283`, M5 of the cold review of 2026-10-03: a crash report, then a refusal to try again.

	A file where the folder should go, or a name the filesystem refused part way through, ended in
	*please report it*. The second left files of lines and no manifest, and a re-run was refused as
	a folder that *already holds something* with nothing to say whose they were.
	"""

	run("init")
	run("add", "Fix the deploy script")
	target = tmp_path / "leaving"

	if where == "a file":
		target.write_text("Somebody's notes.\n", encoding="utf-8")

	else:

		def refused (*_: typing.Any, **__: typing.Any) -> int:
			"""Refuse the readable copy as a filesystem refuses a name too long for it."""

			raise OSError(36, "File name too long", str(target / "markdown" / ("x" * 300)))

		monkeypatch.setattr(subroutine.cli.export, "_markdown", refused)

	result = run("export", str(target), expect=1)
	said = " ".join(result.output.split())

	assert not isinstance(result.exception, OSError), result.exception
	assert "Something went wrong" not in said and "could not be written to" in said, said

	if where == "a file":
		assert target.read_text(encoding="utf-8") == "Somebody's notes.\n"

		return

	assert "unfinished export and can be deleted" in said, said

	again = " ".join(run("export", str(target), expect=1).output.split())

	assert "already holds something" in again and "did not finish" in again, again


def test_a_folder_that_cannot_be_looked_in_is_refused_rather_than_crashing (
	run: typing.Callable[..., typer.testing.Result], tmp_path: pathlib.Path
) -> None:
	"""`SR#4414`, R2-L8 of the cold review of 2026-10-04: the refusal itself crashed.

	Asked to export beneath a folder this account may not enter, the first error was caught, and
	the refusal then looked in the folder to say what it held and raised a second.
	"""

	if os.geteuid() == 0:
		pytest.skip("root enters every folder, so nothing here is refused")

	run("init", "--workspace", "Acme")
	shut = tmp_path / "shut"
	shut.mkdir()
	shut.chmod(0o600)

	try:
		result = run("export", str(shut / "leaving"), expect=1)

	finally:
		shut.chmod(0o700)

	said = " ".join(result.output.split())

	assert not isinstance(result.exception, OSError), result.exception
	assert "Something went wrong" not in said and "could not be written" in said, said
	assert "unfinished" not in said, said


def test_a_folder_of_somebody_s_own_is_never_called_an_unfinished_export (
	run: typing.Callable[..., typer.testing.Result], tmp_path: pathlib.Path
) -> None:
	"""`SR#4414`, R2-L9: a folder holding a file of lines and no manifest was theirs to delete.

	Measured on a folder holding ``sensor-log.jsonl`` and ``notes.txt``, under the name a new
	workspace takes. **Only a folder holding nothing but what an export writes is called one.**
	"""

	run("init", "--workspace", "Acme")
	mine = tmp_path / "mine" / "acme"
	mine.mkdir(parents=True)
	(mine / "sensor-log.jsonl").write_text('{"reading": 21.5}\n', encoding="utf-8")
	(mine / "notes.txt").write_text("Somebody's notes.\n", encoding="utf-8")

	said = " ".join(run("export", str(tmp_path / "mine"), expect=1).output.split())

	assert "already holds something" in said, said
	assert "did not finish" not in said and "can be deleted" not in said, said


class _Failing:
	"""A client that answers as another does, until it stops answering at one kind."""

	def __init__ (self, inner: subroutine.clients.base.Client, kind: str) -> None:
		"""Wrap ``inner``, failing at ``kind``."""

		self.inner = inner
		self.kind = kind

	def export (self, kind: str, *, workspace: str | None = None) -> typing.Iterator[typing.Any]:
		"""Stop at one kind, as an instance that went away part way does."""

		if kind == self.kind:
			raise subroutine.errors.ServiceUnavailable("The instance stopped answering.")

		return self.inner.export(kind, workspace=workspace)

	def __getattr__ (self, name: str) -> typing.Any:
		"""Answer everything else as the wrapped client does."""

		return getattr(self.inner, name)


def test_an_export_refused_part_way_says_what_it_left (pair: Pair, tmp_path: pathlib.Path) -> None:
	"""`SR#4414`, R2-L9: refused part way, the files written so far were left with nothing said."""

	pair.local.capture(text="Fix the deploy script")
	workspace = pair.local.identity().workspaces[0]
	folder = tmp_path / workspace.slug

	with pytest.raises(subroutine.errors.ServiceUnavailable) as refused:
		subroutine.cli.export.write(
			typing.cast(typing.Any, _Failing(pair.local, "comments")),
			folder,
			workspace=workspace,
			connection="work",
		)

	assert (folder / "tasks.jsonl").exists(), sorted(path.name for path in folder.iterdir())
	assert refused.value.hint is not None and "unfinished export" in refused.value.hint, (
		refused.value.hint
	)


def test_an_export_never_writes_over_what_is_there (
	run: typing.Callable[..., typer.testing.Result], tmp_path: pathlib.Path
) -> None:
	"""A second export into the same place is refused by name, and the first is untouched."""

	run("init")
	run("add", "Fix the deploy script")
	run("export", str(tmp_path / "leaving"))

	folder = next((tmp_path / "leaving").iterdir())
	before = (folder / "manifest.json").read_text(encoding="utf-8")
	again = " ".join(run("export", str(tmp_path / "leaving"), expect=1).output.split())

	assert "already holds something" in again, again
	assert (folder / "manifest.json").read_text(encoding="utf-8") == before


class _Refusing:
	"""A client that answers as another does, except that it refuses one kind."""

	def __init__ (self, inner: subroutine.clients.base.Client, refused: str) -> None:
		"""Wrap ``inner``, refusing ``refused``."""

		self.inner = inner
		self.refused = refused

	def export (self, kind: str, *, workspace: str | None = None) -> typing.Iterator[typing.Any]:
		"""Refuse one kind, as a credential without its permission is refused it."""

		if kind == self.refused:
			raise subroutine.errors.Forbidden("This credential may not read comments.")

		return self.inner.export(kind, workspace=workspace)

	def __getattr__ (self, name: str) -> typing.Any:
		"""Answer everything else as the wrapped client does."""

		return getattr(self.inner, name)


class _PartWay:
	"""A client that answers two rows of one kind and is then refused the rest."""

	def __init__ (self, inner: subroutine.clients.base.Client, refused: str) -> None:
		"""Wrap ``inner``, refusing ``refused`` after its second row."""

		self.inner = inner
		self.refused = refused

	def export (self, kind: str, *, workspace: str | None = None) -> typing.Iterator[typing.Any]:
		"""Answer as the wrapped client does, until the third row of the refused kind."""

		for count, item in enumerate(self.inner.export(kind, workspace=workspace)):
			if kind == self.refused and count == 2:
				raise subroutine.errors.Forbidden("This credential may no longer read items.")

			yield item

	def __getattr__ (self, name: str) -> typing.Any:
		"""Answer everything else as the wrapped client does."""

		return getattr(self.inner, name)


class _Older:
	"""A client for an instance released before export: no route, and an older version."""

	def __init__ (self, inner: subroutine.clients.base.Client) -> None:
		"""Wrap ``inner``."""

		self.inner = inner

	def export (self, kind: str, *, workspace: str | None = None) -> typing.Iterator[typing.Any]:
		"""Answer as a server with no such route does."""

		raise subroutine.errors.NotFound(f"There is nothing at /v1/export/{kind}.")

	def me (self) -> subroutine.views.Me:
		"""Say the instance runs the last release before export."""

		return self.inner.me().model_copy(update={"instance_version": "0.9.13"})

	def __getattr__ (self, name: str) -> typing.Any:
		"""Answer everything else as the wrapped client does."""

		return getattr(self.inner, name)


def test_a_kind_refused_part_way_leaves_no_pages (pair: Pair, tmp_path: pathlib.Path) -> None:
	"""`SR#4317`: two rows read before the refusal still became pages, beside *refused: tasks*."""

	for title in ("Fix the deploy script", "Ring the dentist", "Take the red pill"):
		pair.local.capture(text=title)

	workspace = pair.local.identity().workspaces[0]
	written = subroutine.cli.export.write(
		typing.cast(typing.Any, _PartWay(pair.local, "tasks")),
		tmp_path / workspace.slug,
		workspace=workspace,
		connection="local",
	)

	assert written.refused == {"tasks": "This credential may no longer read items."}
	assert written.pages == 0, "pages were written for a kind the manifest calls refused"


def test_an_instance_older_than_export_is_named_and_leaves_nothing (
	pair: Pair, tmp_path: pathlib.Path
) -> None:
	"""`SR#4317`: a 0.9.13 server said only that nothing was at ``/v1/export/workspace``.

	And left an empty ``workspace.jsonl``, so the next export into the same place was refused.
	"""

	workspace = pair.local.identity().workspaces[0]
	folder = tmp_path / workspace.slug

	with pytest.raises(subroutine.errors.NotFound) as refused:
		subroutine.cli.export.write(
			typing.cast(typing.Any, _Older(pair.local)), folder, workspace=workspace, connection="work"
		)

	assert "runs Subroutine 0.9.13, and export needs 0.10.0 or later" in refused.value.detail
	assert not any(folder.iterdir()), sorted(path.name for path in folder.iterdir())


def test_w_names_the_workspace_to_export_as_every_command_does (
	run: typing.Callable[..., typer.testing.Result], tmp_path: pathlib.Path
) -> None:
	"""`SR#4317`: ``-w METACORTEX export`` found nothing, where ``-w METACORTEX list`` works."""

	run("init", "--workspace", "Metacortex")
	run("add", "Fix the deploy script")

	said = " ".join(run("-w", "METACORTEX", "export", str(tmp_path / "leaving")).output.split())

	assert "Exported metacortex to" in said, said


@pytest.mark.parametrize("slug", ["../../escaped", "<absolute>", "", "My_Team", "a/b"])
def test_a_workspace_name_no_instance_makes_stops_the_export (
	slug: str, pair: Pair, tmp_path: pathlib.Path
) -> None:
	"""A short name that is not its own normal form is refused by name, before any folder is made.

	`#4279` (H2 of the cold review of 2026-10-03): the folder was ``directory / slug``, so a
	server sending ``../../escaped`` or an absolute path had the whole export written there.
	"""

	slug = slug.replace("<absolute>", str(tmp_path / "escaped"))
	workspace = pair.local.identity().workspaces[0].model_copy(update={"slug": slug})

	with pytest.raises(subroutine.errors.ServiceUnavailable) as refused:
		subroutine.cli.export.folder_for(tmp_path / "leaving", workspace, connection="work")

	assert "work sent a workspace name no instance makes" in refused.value.detail
	assert list(tmp_path.iterdir()) == []


def test_an_ordinary_workspace_name_is_its_own_folder (pair: Pair, tmp_path: pathlib.Path) -> None:
	"""The check refuses nothing an instance makes."""

	workspace = pair.local.identity().workspaces[0]

	assert subroutine.cli.export.folder_for(
		tmp_path, workspace, connection="local"
	) == tmp_path / workspace.slug


class _Climbing:
	"""A client that answers as another does, except that every project path it sends is ``path``."""

	def __init__ (self, inner: subroutine.clients.base.Client, path: str) -> None:
		"""Wrap ``inner``, sending ``path`` as every item's project path."""

		self.inner = inner
		self.path = path

	def export (self, kind: str, *, workspace: str | None = None) -> typing.Iterator[typing.Any]:
		"""Rewrite each item's and document's project path, as a hostile server would."""

		for item in self.inner.export(kind, workspace=workspace):
			if kind in subroutine.cli.export.FRONT:
				item = item.model_copy(update={"project_path": self.path})

			yield item

	def __getattr__ (self, name: str) -> typing.Any:
		"""Answer everything else as the wrapped client does."""

		return getattr(self.inner, name)


@pytest.mark.parametrize("path", ["../../../escaped", "<absolute>", "inbox/../..", "", "inbox/"])
def test_a_project_path_no_instance_makes_stops_the_export_before_any_page (
	path: str, pair: Pair, tmp_path: pathlib.Path
) -> None:
	"""Refused by name, with no page written anywhere and no manifest saying the export finished.

	`#4279`: a page went to ``markdown / project_path``, so a server sending ``../../../escaped``
	or an absolute path had pages, with content it chose, written outside the export.
	"""

	path = path.replace("<absolute>", str(tmp_path / "escaped"))
	pair.local.capture(text="Fix the deploy script")
	workspace = pair.local.identity().workspaces[0]
	folder = tmp_path / "leaving" / workspace.slug

	with pytest.raises(subroutine.errors.ServiceUnavailable) as refused:
		subroutine.cli.export.write(
			typing.cast(typing.Any, _Climbing(pair.local, path)),
			folder,
			workspace=workspace,
			connection="work",
		)

	assert "work sent a project path no instance makes" in refused.value.detail
	assert not (folder / "markdown").exists()
	assert not (folder / "manifest.json").exists()
	assert sorted(entry.name for entry in tmp_path.iterdir()) == ["leaving"]


def test_a_kind_the_credential_is_refused_is_named_in_the_manifest_and_not_written (
	pair: Pair, tmp_path: pathlib.Path
) -> None:
	"""Left out and said, never an empty file that reads as *there were none*."""

	pair.local.capture(text="Fix the deploy script")
	workspace = pair.local.identity().workspaces[0]
	written = subroutine.cli.export.write(
		typing.cast(typing.Any, _Refusing(pair.local, "comments")),
		tmp_path / workspace.slug,
		workspace=workspace,
		connection="local",
	)
	manifest = json.loads((written.folder / "manifest.json").read_text(encoding="utf-8"))

	assert not (written.folder / "comments.jsonl").exists()
	assert manifest["refused"] == {"comments.jsonl": "This credential may not read comments."}
	assert "comments.jsonl" not in manifest["files"]
	assert any("No comments: This credential may not read comments." in line for line in (
		subroutine.cli.export.described(workspace, written)
	))
