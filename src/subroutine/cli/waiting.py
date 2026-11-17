"""Ask whether standard input would answer at once, and read a socket to its end - `SR#5303`.

Decision `#5285`: a question is put to input that is not a terminal only where an answer is
already waiting, and text at ``-`` is read from a socket only where something is already in it,
each read then waiting a bounded time for more.

**A module of its own because ``select`` is a word another guard reads.** ``tests/test_scoping.py``
counts any ``.select`` in a module naming a task, a project or a document as a database query
that must narrow through :mod:`subroutine.domain.scoping`, and ``select.select`` asks a
descriptor, not a database. Here it names neither, which is truer than an exemption written for
``cli/personal.py`` that would also excuse a real query there.
"""

import os
import select
import stat


def ready (descriptor: int, mode: int) -> bool:
	"""Say whether a read of ``descriptor`` would return at once, with something or at its end.

	**Where that cannot be asked**, as of a pipe on Windows, a pipe or a file is taken as it was
	before `SR#5303` and a socket is not. ``mode`` is the descriptor's, from ``os.fstat``.
	"""

	try:
		answered, _writing, _failing = select.select([descriptor], [], [], 0)

	except (OSError, ValueError):
		return not stat.S_ISSOCK(mode)

	return bool(answered)


def read_to_the_end (descriptor: int, seconds: float) -> bytes | None:
	"""Return everything ``descriptor`` holds, read to its end, or ``None`` where it stalls.

	Each read waits at most ``seconds`` for more, so input that stops arriving that long without
	ending gives ``None``, and what had arrived is not returned: a part of a text is not the text.
	"""

	chunks: list[bytes] = []

	while True:
		answered, _writing, _failing = select.select([descriptor], [], [], seconds)

		if not answered:
			return None

		chunk = os.read(descriptor, 65536)

		if not chunk:
			return b"".join(chunks)

		chunks.append(chunk)
