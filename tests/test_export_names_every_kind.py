"""Every kind an export writes is named in the summary the terminal prints - `SR#5287`.

``cli/export.py``'s ``NOUNS`` is the third declaration of the kinds, beside ``exporting.KINDS``
and ``views.EXPORTED``, and the only one nothing held to the others. ``users_beyond_reach`` was
added to both of those and never to it, so every export naming an account out of its reader's
reach crashed after writing its files, and so did every export from a 0.10.x instance, which
does not have that kind (H-1 of the cold review of 2026-11-11).
"""

import pathlib
import uuid

import pytest

import subroutine.cli.export
import subroutine.views


def test_every_kind_exported_has_a_noun () -> None:
	"""The summary can name every kind the client asks an instance for."""

	assert set(subroutine.cli.export.NOUNS) == set(subroutine.views.EXPORTED)


def _workspace () -> subroutine.views.WorkspaceRef:
	"""Return the workspace the summaries below describe."""

	return subroutine.views.WorkspaceRef(id=uuid.uuid4(), slug="metacortex", title="Metacortex")


@pytest.mark.parametrize("count", [1, 2])
def test_every_kind_counted_is_said (tmp_path: pathlib.Path, count: int) -> None:
	"""Each kind held once or twice is said on the count line, bar the workspace's own words."""

	written = subroutine.cli.export.Written(
		folder=tmp_path, counts=dict.fromkeys(subroutine.views.EXPORTED, count), refused={}
	)

	said = "\n".join(subroutine.cli.export.described(_workspace(), written))

	for kind, (one, many) in subroutine.cli.export.NOUNS.items():
		if kind in subroutine.cli.export.WORDS:
			continue

		assert f"{count} {one if count == 1 else many}" in said, kind


def test_every_kind_refused_or_unavailable_is_said (tmp_path: pathlib.Path) -> None:
	"""A kind refused, or one an older instance does not export, is named on a line of its own."""

	kinds = list(subroutine.views.EXPORTED)
	written = subroutine.cli.export.Written(
		folder=tmp_path,
		counts={},
		refused=dict.fromkeys(kinds, "you may not read them."),
		unavailable=dict.fromkeys(kinds, "not found"),
	)

	said = subroutine.cli.export.described(_workspace(), written)

	for kind in kinds:
		many = subroutine.cli.export.NOUNS[kind][1]

		assert f"  No {many}: you may not read them." in said, kind
		assert f"  No {many}: this instance does not export them." in said, kind
