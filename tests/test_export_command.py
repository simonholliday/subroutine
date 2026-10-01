"""``subroutine export``: a folder per workspace, a file per kind, the manifest last - `#4053`.

Decision `#4049`. Driven through the real command against a fresh instance, as the personal
path's tests are, and through :func:`subroutine.cli.export.write` with a client that refuses one
kind, which a person's own instance never does.
"""

import datetime
import json
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
