"""``subroutine.osc``: an OSC message's bytes, where to send it, and the sender - item ``#2722``.

**The encoder is held to bytes python-osc wrote**, the library the sister apps send and listen with,
so what Subroutine sends is what they expect - with no dependency on it at run time, and none in
the tests either (Simon's decision 1 on ``#2721``: our own encoder, python-osc only as the check).

**The sender is held to *fire and forget*** (Simon, 2026-09-25): whoever hands a datagram over
never waits - not for a name to be looked up, and not for room in a full queue - and a failure
costs the one datagram it happened to.
"""

import contextlib
import socket
import threading
import time
import typing

import pytest

import subroutine.osc

#: Messages, and the bytes python-osc 1.10.2's ``OscMessageBuilder`` wrote for each, integers added
#: as ``i`` and strings as ``s``, on 2026-09-25. **Chosen for their edges**: no values at all; a
#: string that fills its four bytes and so takes four more; an empty one; text beyond ASCII; and
#: both ends of the 32-bit range.
WRITTEN_BY_PYTHON_OSC: tuple[tuple[str, tuple[subroutine.osc.Value, ...], str], ...] = (
	(
		"/subroutine/bug/filed",
		(3042, 4, 2, "subsample", "person"),
		"2f737562726f7574696e652f6275672f66696c65640000002c6969697373000000000be2000000040000"
		"000273756273616d706c65000000706572736f6e0000",
	),
	(
		"/subroutine/milestone/done",
		(3602, 0, 0, "subroutine", "agent", "Release 0.9.7"),
		"2f737562726f7574696e652f6d696c6573746f6e652f646f6e6500002c6969697373730000000e1200000000"
		"00000000737562726f7574696e6500006167656e7400000052656c6561736520302e392e37000000",
	),
	(
		"/subroutine/workspace/seeded",
		(),
		"2f737562726f7574696e652f776f726b73706163652f736565646564000000002c000000",
	),
	(
		"/sub",
		("abcd", "", 0),
		"2f737562000000002c7373690000000061626364000000000000000000000000",
	),
	(
		"/subroutine/task/filed",
		(1, 0, 0, "subroutine/ui", "", "Café ☕ and 2,147,483,647"),
		"2f737562726f7574696e652f7461736b2f66696c656400002c69696973737300000000010000000000000000"
		"737562726f7574696e652f756900000000000000436166c3a920e2989520616e6420322c3134372c3438332c"
		"36343700",
	),
	(
		"/subroutine/task/filed",
		(2147483647, -1, 0, "", "person"),
		"2f737562726f7574696e652f7461736b2f66696c656400002c696969737300007fffffffffffffff00000000"
		"00000000706572736f6e0000",
	),
)


@pytest.mark.parametrize(
	("address", "values", "written"),
	WRITTEN_BY_PYTHON_OSC,
	ids=[f"{address} {values}" for address, values, _ in WRITTEN_BY_PYTHON_OSC],
)
def test_a_message_is_the_bytes_python_osc_writes (
	address: str, values: tuple[subroutine.osc.Value, ...], written: str
) -> None:
	"""The sister apps listen with python-osc, so its bytes are the contract."""

	assert subroutine.osc.encode(address, values).hex() == written


@pytest.mark.parametrize(
	("written", "read"),
	[
		("studio.local:9000", ("studio.local", 9000)),
		(" 192.168.0.20:9000 ", ("192.168.0.20", 9000)),
		("[::1]:9000", ("::1", 9000)),
		# The way OSC tools write a destination, which somebody may paste from one.
		("osc.udp://studio.local:9000", ("studio.local", 9000)),
		("OSC.UDP://10.0.0.2:57120/", ("10.0.0.2", 57120)),
	],
)
def test_a_destination_is_read_in_every_form_it_is_written (
	written: str, read: tuple[str, int]
) -> None:
	"""A machine's name or address and a port, with or without the prefix OSC tools write."""

	assert subroutine.osc.destination(written) == read


@pytest.mark.parametrize(
	("written", "said"),
	[
		("studio.local", "ends in no port"),
		("studio.local:0", "ends in no port"),
		("studio.local:65536", "ends in no port"),
		("studio.local:90a0", "ends in no port"),
		(":9000", "names no machine"),
		("my studio:9000", "names no machine"),
		("http://studio.local:9000", "is a web address"),
		("::1:9000", "written in brackets"),
	],
)
def test_a_destination_that_is_not_one_is_refused_saying_why (written: str, said: str) -> None:
	"""**The reason is the one that is true**: a missing port is not called a missing machine."""

	with pytest.raises(ValueError, match=said):
		subroutine.osc.destination(written)


def test_a_datagram_reaches_a_socket_that_is_listening () -> None:
	"""The whole of the wire, on this machine: handed over, sent, and heard."""

	with _listening() as (listener, port):
		subroutine.osc.Sender().send("127.0.0.1", port, [b"one", b"two"])

		assert {_heard(listener), _heard(listener)} == {b"one", b"two"}


def test_a_name_is_looked_up_on_the_senders_thread_and_never_the_callers () -> None:
	"""**A lookup that takes its time holds nothing up**, which a stopwatch alone cannot show.

	The stand-in resolver blocks until released, as a name nobody answers for does, and records
	which thread asked: the caller must be back before it is released, and must not be that
	thread.
	"""

	release = threading.Event()
	asked: list[threading.Thread] = []

	def resolve (host: str, port: int, **_: typing.Any) -> list[typing.Any]:
		"""Block like a slow lookup, and fail, which is the sender's to swallow."""

		asked.append(threading.current_thread())
		release.wait(5)

		raise OSError("no such name")

	sender = subroutine.osc.Sender(resolve=resolve)
	started = time.monotonic()
	sender.send("nowhere.invalid", 9000, [b"x"])
	returned = time.monotonic() - started

	_until(lambda: bool(asked))
	release.set()

	assert returned < 0.5, f"handing over took {returned:.2f}s, so the caller waited"
	assert asked and asked[0] is not threading.current_thread()


def test_what_does_not_fit_is_dropped_rather_than_waited_for () -> None:
	"""A full queue drops the tail of a burst, and the caller never waits for room.

	The stand-in resolver holds the sender's thread on its first datagram until the whole burst
	has been handed over, so what gets through is what the queue could hold.
	"""

	release = threading.Event()

	with _listening() as (listener, port):

		def resolve (host: str, asked: int, **_: typing.Any) -> list[typing.Any]:
			"""Hold the first lookup, then send everything to the listening socket."""

			release.wait(5)

			return [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("127.0.0.1", port))]

		sender = subroutine.osc.Sender(waiting=2, resolve=resolve)
		started = time.monotonic()
		sender.send("studio.local", 9000, [bytes([one]) for one in range(10)])
		returned = time.monotonic() - started
		release.set()
		sender.drain(2)
		listener.settimeout(0.5)
		heard: list[bytes] = []

		with contextlib.suppress(TimeoutError):
			while True:
				heard.append(_heard(listener))

	assert returned < 0.5, f"a burst took {returned:.2f}s to hand over"
	assert 2 <= len(heard) <= 3, f"{len(heard)} of 10 went, through a queue that holds 2"


def test_a_failure_costs_its_own_datagram_and_the_next_still_goes () -> None:
	"""A name that does not resolve drops one datagram, and the sender carries on."""

	def resolve (host: str, port: int, **options: typing.Any) -> list[typing.Any]:
		"""Fail for one name, as a machine that is off or misspelled does, and look up the rest."""

		if host == "nowhere":
			raise OSError("no such name")

		return socket.getaddrinfo(host, port, **options)

	with _listening() as (listener, port):
		sender = subroutine.osc.Sender(resolve=resolve)
		sender.send("nowhere", 9000, [b"lost"])
		sender.send("127.0.0.1", port, [b"heard"])

		assert _heard(listener) == b"heard"


@contextlib.contextmanager
def _listening () -> typing.Iterator[tuple[socket.socket, int]]:
	"""Open a UDP socket on a free port of this machine for as long as a test needs it."""

	listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

	try:
		listener.bind(("127.0.0.1", 0))
		listener.settimeout(5)

		yield listener, listener.getsockname()[1]

	finally:
		listener.close()


def _heard (listener: socket.socket) -> bytes:
	"""Return the next datagram a socket receives, or fail after its timeout."""

	datagram, _ = listener.recvfrom(65_535)

	return datagram


def _until (condition: typing.Callable[[], bool], seconds: float = 5) -> None:
	"""Wait for a condition another thread makes true, failing rather than waiting for ever."""

	deadline = time.monotonic() + seconds

	while not condition():
		assert time.monotonic() < deadline, "the sender's thread never got there"

		time.sleep(0.005)
