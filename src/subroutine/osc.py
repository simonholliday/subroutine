"""Open Sound Control, sent and forgotten - design `#2721`, item `#2722`.

**OSC is how music software talks to itself over a network.** A message is an *address*, written
like a path - ``/subroutine/bug/filed`` - and a few values, in one UDP datagram that nothing
answers. This module is only the wire: it writes a message's bytes, reads where a workspace said
to send them, and sends them from a thread of its own. What an event *says* is
:mod:`subroutine.domain.sounds`'s.

**Fire and forget** (Simon, 2026-09-25): *we don't care if it fails, and it never holds anything
up or breaks anything on a fail.* So nothing here raises into whoever hands a message over, a name
is looked up on the sender's thread rather than theirs, a queue that is full drops what does not
fit, and nothing is ever tried twice.
"""

import atexit
import contextlib
import queue
import socket
import struct
import threading
import time
import typing

#: How many datagrams may wait for the sender. **A burst beyond it loses its tail rather than
#: memory** (Simon's decision 9 on `#2721`: every event is sent, as it happens).
WAITING = 1_024

#: How long a looked-up name is trusted, in seconds - long enough that a busy afternoon does not
#: ask the network for the same name on every event, and short enough that a music machine given a
#: new address is found again.
REMEMBERED = 60.0

#: How long a command that is finishing waits for what it handed over, in seconds. A command run
#: against a local instance exits straight after its write, and the sender's thread goes with it.
AT_EXIT = 0.25

#: The prefix OSC tools write before a destination, accepted and set aside.
SCHEME = "osc.udp://"

#: What a message carries: OSC's 32-bit integers and its strings, which is everything
#: :mod:`subroutine.domain.sounds` sends.
Value = int | str


def encode (address: str, values: typing.Sequence[Value]) -> bytes:
	"""Return one OSC 1.0 message as the bytes its datagram carries.

	**Byte for byte what python-osc writes** for the same message, which is what the sister apps
	send and listen with: ``tests/test_osc.py`` holds bytes python-osc wrote. An address, then a
	type tag naming each value - ``i`` for a 32-bit integer, ``s`` for a string - then the values,
	each integer big-endian and each string ended by a zero byte and padded to a multiple of four.
	"""

	tags = "," + "".join("i" if isinstance(value, int) else "s" for value in values)
	body = b"".join(
		struct.pack(">i", value) if isinstance(value, int) else _padded(value) for value in values
	)

	return _padded(address) + _padded(tags) + body


def destination (written: str) -> tuple[str, int]:
	"""Read where a workspace said to send, as a host and a port, or raise :class:`ValueError`.

	``studio.local:9000``, ``192.168.0.20:9000`` or ``[::1]:9000``, and any of them written the way
	OSC tools write a destination: ``osc.udp://studio.local:9000``. **A name is checked for its
	shape and never looked up here**, because whether it answers is the sender's to find out,
	later and on a thread nobody waits for.
	"""

	text = written.strip()

	if text.lower().startswith(SCHEME):
		text = text[len(SCHEME):].rstrip("/")

	if "://" in text:
		raise ValueError(f"{written!r} is a web address, and OSC goes to a machine and a port.")

	if text.startswith("["):
		host, closed, rest = text[1:].partition("]")
		port = rest[1:] if closed and rest.startswith(":") else ""
		bracketed = True

	else:
		# **No colon is a machine with no port**, not a port with no machine, which is what
		# `rpartition` alone would make of it.
		host, _, port = text.rpartition(":") if ":" in text else (text, "", "")
		bracketed = False

	if not host or any(char.isspace() or char in "/@" for char in host):
		raise ValueError(f"{written!r} names no machine before its port.")

	if ":" in host and not bracketed:
		raise ValueError(f"{written!r} holds an IPv6 address, which is written in brackets.")

	if not port.isdigit() or not 1 <= int(port) <= 65_535:
		raise ValueError(f"{written!r} ends in no port, which is a number from 1 to 65535.")

	return host, int(port)


class Sender:
	"""Sends datagrams from a thread of its own, so whoever hands one over never waits for it."""

	def __init__ (
		self,
		*,
		waiting: int = WAITING,
		resolve: typing.Callable[..., typing.Any] = socket.getaddrinfo,
	) -> None:
		"""Make a sender whose thread starts with the first datagram handed to it.

		``resolve`` is :func:`socket.getaddrinfo` except where a test asks where a name was looked
		up, which is the half of *never holds anything up* a stopwatch cannot see.
		"""

		self._queue: queue.Queue[tuple[str, int, bytes]] = queue.Queue(maxsize=waiting)
		self._resolve = resolve
		self._lock = threading.Lock()
		self._thread: threading.Thread | None = None
		self._leaving = False
		self._known: dict[tuple[str, int], tuple[float, int, typing.Any]] = {}
		self._sockets: dict[int, socket.socket] = {}

	def send (self, host: str, port: int, datagrams: typing.Iterable[bytes]) -> None:
		"""Hand datagrams over for one destination and return at once.

		**What does not fit is dropped**, since a queue that grew without bound would turn a burst
		into memory, and a caller that waited for room would be held up by the network after all.
		"""

		self._started()

		for datagram in datagrams:
			try:
				self._queue.put_nowait((host, port, datagram))

			except queue.Full:
				return

	def drain (self, seconds: float = AT_EXIT) -> None:
		"""Wait at most ``seconds`` for what was handed over to go, for a command that is ending."""

		deadline = time.monotonic() + seconds

		while self._queue.unfinished_tasks and time.monotonic() < deadline:
			time.sleep(0.005)

	def _started (self) -> None:
		"""Start the thread that sends, once, and have a finishing command give it a moment."""

		with self._lock:
			if self._thread is not None:
				return

			self._thread = threading.Thread(target=self._run, name="subroutine-osc", daemon=True)
			self._thread.start()

			if not self._leaving:
				atexit.register(self.drain)
				self._leaving = True

	def _run (self) -> None:
		"""Send whatever arrives, for as long as the process lives."""

		while True:
			host, port, datagram = self._queue.get()

			# **Anything at all is dropped**: a name that does not resolve, a machine that is off, a
			# network that refuses. That is the whole of *fire and forget*, and it is why nothing
			# here is logged either - a machine that is off would otherwise write a line per event.
			with contextlib.suppress(Exception):
				self._sent(host, port, datagram)

			self._queue.task_done()

	def _sent (self, host: str, port: int, datagram: bytes) -> None:
		"""Send one datagram, looking its destination up if it has not been lately."""

		family, address = self._found(host, port)
		connection = self._sockets.get(family)

		if connection is None:
			connection = socket.socket(family, socket.SOCK_DGRAM)
			connection.setblocking(False)
			self._sockets[family] = connection

		connection.sendto(datagram, address)

	def _found (self, host: str, port: int) -> tuple[int, typing.Any]:
		"""Return where a host and port are on the network, from memory where it is recent."""

		now = time.monotonic()
		known = self._known.get((host, port))

		if known is not None and now - known[0] < REMEMBERED:
			return known[1], known[2]

		family, _, _, _, address = self._resolve(host, port, type=socket.SOCK_DGRAM)[0]
		self._known[(host, port)] = (now, family, address)

		return family, address


#: The one sender a process has. Its thread starts with the first datagram, so a process that
#: never sends never has one.
SENDER = Sender()


def _padded (text: str) -> bytes:
	"""Return a string as OSC writes one: UTF-8, a zero byte, and zeros to a multiple of four."""

	written = text.encode("utf-8") + b"\x00"

	return written + b"\x00" * (-len(written) % 4)
