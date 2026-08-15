"""Make this checkout's git hooks runnable, on a filesystem that will not let them be.

The hooks themselves are tracked, in ``hooks/``. This puts a **shim** for each one somewhere
the operating system will execute, and points ``core.hooksPath`` at the shims.

**That indirection exists because of a measurement, not a preference.** This repository lives
on a CIFS share mounted ``file_mode=0666``, which forces every file to be non-executable — the
mount option is root's and no ``chmod`` can defeat it. A hook placed anywhere inside the
working tree, ``.git/hooks`` included, is refused with ``Permission denied`` (exit 126,
measured both ways on 2026-08-15). And **git skips a hook it cannot execute without saying
so**, which is the worst available failure: the install reports success, nothing runs, and the
only evidence is an absence — item ``#236``'s shape, one layer down.

A shim rather than a copy, deliberately. A copy is a cache, and a cache that lags its source
is ``#380``: editing ``hooks/commit-msg`` would change nothing until somebody remembered to
re-install, with both halves reporting success throughout. Each shim runs the tracked file
through ``sh`` by path, so the tracked file is always the one that runs and never needs to be
executable itself.

Run it once per clone:

    python scripts/install_hooks.py
"""

import os
import pathlib
import stat
import subprocess
import sys
import typing

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Where the shims go. State rather than cache: losing them silently disables every hook, and
#: a cache is a thing the system may clear whenever it likes.
def shim_directory () -> pathlib.Path:
	"""Return the directory the shims are installed into."""

	state = os.environ.get("XDG_STATE_HOME") or (pathlib.Path.home() / ".local" / "state")

	return pathlib.Path(state) / "subroutine-git-hooks"


def sources () -> list[pathlib.Path]:
	"""Return the tracked hooks, discovered rather than listed.

	A list here would be a second place to add a hook, and the one somebody forgets.
	"""

	return sorted(path for path in (ROOT / "hooks").iterdir() if path.is_file())


def _shim (source: pathlib.Path) -> str:
	"""Return a shim that runs one tracked hook wherever it currently is."""

	return (
		"#!/bin/sh\n"
		"# Written by scripts/install_hooks.py. Runs the tracked hook, which is the one that\n"
		"# matters — edit that, not this.\n"
		f'exec /bin/sh "{source}" "$@"\n'
	)


def _executable (path: pathlib.Path) -> bool:
	"""Report whether the operating system will actually run this file.

	``os.access`` rather than reading the mode, because the question is what happens at
	``exec`` time and a filesystem may answer differently from what ``stat`` suggests.
	"""

	return os.access(path, os.X_OK)


def install (into: pathlib.Path | None = None) -> list[str]:
	"""Install a shim per tracked hook and point git at them. Returns what was written."""

	directory = into or shim_directory()
	directory.mkdir(parents=True, exist_ok=True)

	written = []

	for source in sources():
		shim = directory / source.name
		shim.write_text(_shim(source))
		shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

		# **Checked rather than assumed, which is the whole reason this script exists.** If the
		# state directory is itself somewhere that cannot execute — a second network mount, a
		# `noexec` partition — then installing here has produced the same silent nothing, and
		# saying so now is the difference between a minute and an afternoon.
		if not _executable(shim):
			raise SystemExit(
				f"{shim} was written and cannot be executed, so git would skip it without "
				f"a word. Choose somewhere that allows execution:\n"
				f"    XDG_STATE_HOME=<somewhere> python scripts/install_hooks.py"
			)

		written.append(source.name)

	subprocess.run(
		["git", "config", "core.hooksPath", str(directory)],
		cwd=ROOT,
		check=True,
	)

	return written


def main (argv: typing.Sequence[str] | None = None) -> int:
	"""Install the hooks and say what will now happen."""

	written = install()

	print(f"Installed {len(written)} hooks: {', '.join(written)}")
	print(f"  shims in   {shim_directory()}")
	print(f"  running    {ROOT / 'hooks'}")
	print()
	print("A commit message now has to cite an item that exists, and the commit is recorded")
	print("against every item it cites. 'git commit --no-verify' skips the first.")

	return 0


if __name__ == "__main__":
	sys.exit(main())
