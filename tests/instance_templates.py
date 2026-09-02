"""One initialised instance per shape, built once and copied, rather than rebuilt per test.

**`subroutine init` is 456 ms and copying its result is 0.5 ms** — 850x, measured on this
workstation, best of five each. The suite ran it **479 times** to produce a byte-identical
result, which is about 220 seconds of CPU, **9% of the whole suite** (`SR#1830`, off `SR#1725`'s
measurement). Two files carry 410 of those calls and are 37% of the suite's CPU between them.

**Where the 456 ms goes, measured rather than assumed**: 696 ms of a 782 ms profiled run is
:func:`subroutine.db.migrate.upgrade`, and 571 ms of *that* is 110 ``batch_alter_table`` calls —
SQLite rebuilding a table by copy-drop-rename, plus 324 ms of schema reflection. An empty
database has nothing to migrate; what it is paying for is the replay of this project's whole
schema history.

**So why not build the schema directly and stamp it.** ``Base.metadata.create_all`` plus
``alembic stamp head`` would skip the replay for a real ``init`` as well, and it is refused:
Alembic's autogenerate **does not compare CHECK constraints**, and the status categories and
entity-type vocabularies live in them — which is why
:func:`subroutine.db.migrate.check_constraint_differences` exists at all. A tree copied from a
genuinely migrated database carries the schema the migrations actually produce, which is the
property the suite is there to check.

**What makes the copy safe, and each was established rather than assumed:**

- **``init`` writes two files and no absolute path** — a ``config.toml`` holding only a
  ``secret_key``, and the database. So a tree copied to a different place carries nothing that
  names the old one.
- **Nothing in the CLI caches across invocations in one process** keyed on a path. These run
  through ``CliRunner`` in-process, so a module-level cache would have survived the copy; the
  one :func:`functools.cache` in the tree derives from mounted routes.
- **A shared ``instance.id`` cannot reach the tests that need two.**
  ``tests/test_cli_connections.py`` builds a second installation deliberately, and it does so in
  a **subprocess** with its own environment — never through the fixture this module serves.

**The whole falsification is that 479 call sites already assert what a fresh instance looks
like.** ``tests/test_instances_template.py`` is the guard for the substitution itself: for every
shape cached here it builds a real one beside a planted one and holds the two to the same
output, the same file set and the same schema.
"""

import contextlib
import os
import pathlib
import shutil
import typing

import typer.testing

#: The environment variables that decide where an instance's files go.
#:
#: **Read from the environment rather than from a layout of our own**, because the two test
#: files that use this name their directories differently — ``xdg_config_home`` under the test's
#: ``tmp_path`` in one, ``config`` under a ``here`` subdirectory in the other. Copying *from the
#: template's value of a variable to the test's value of the same variable* needs no agreement
#: about either.
VARIABLES = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")


#: The command shapes a template may stand in for.
#:
#: **Spelled out rather than "anything beginning with ``init``"**, so a flag nobody has weighed
#: runs for real. ``--verbose`` prints the database path, which a planted tree would report as
#: the template's; ``--password-stdin`` reads input. Neither is here, and neither had to be
#: excluded by name — **falling through is the default**, and that is what keeps this list from
#: being the thing that has to be right.
#:
#: ``--workspace`` changes what goes into the database and **not what ``init`` prints** —
#: measured, all three say ``Ready. Try: subroutine add "something to do"`` — so a recorded
#: result is honest for each, and each still gets a tree of its own.
CACHEABLE = frozenset(
	{
		("init",),
		("init", "--workspace", "Personal"),
	}
)


class Store:
	"""The initialised trees built so far, keyed by the arguments that produced them.

	One per session per worker. Under ``-n auto`` that is one build per shape per worker rather
	than one per test — a couple of dozen builds instead of four hundred and seventy-nine.
	"""

	def __init__ (self, under: pathlib.Path) -> None:
		"""Keep the templates under a directory that outlives the tests using them."""

		self._under = under
		self._built: dict[
			tuple[str, ...], tuple[dict[str, pathlib.Path], typer.testing.Result]
		] = {}

	def planted (
		self, arguments: tuple[str, ...], invoke: typing.Callable[..., typer.testing.Result]
	) -> typer.testing.Result | None:
		"""Copy an instance of this shape into wherever the environment points, or decline.

		``None`` means *this one has to be run for real*, and the caller runs it. Declining is
		the ordinary answer rather than the exception: a shape nothing caches, a home somebody
		has already written to, or an environment that does not say where the files go.
		"""

		if arguments not in CACHEABLE:
			return None

		where = _destinations()

		if not where or _occupied(where):
			return None

		sources, recorded = self._template(arguments, invoke)

		for name, source in sources.items():
			if source.is_dir():
				shutil.copytree(source, where[name], dirs_exist_ok=True)

		return recorded

	def _template (
		self, arguments: tuple[str, ...], invoke: typing.Callable[..., typer.testing.Result]
	) -> tuple[dict[str, pathlib.Path], typer.testing.Result]:
		"""Return the tree for this shape, building it the first time it is asked for."""

		if arguments in self._built:
			return self._built[arguments]

		root = self._under / f"instance-{len(self._built)}"
		sources = {name: root / name.lower() for name in VARIABLES}

		with _aimed_at(sources):
			recorded = invoke(*arguments)

		assert recorded.exit_code == 0, (
			f"the template for 'subroutine {' '.join(arguments)}' exited "
			f"{recorded.exit_code}\n{recorded.output}\n{recorded.exception!r}"
		)

		self._built[arguments] = (sources, recorded)

		return self._built[arguments]


def caching (
	store: Store, invoke: typing.Callable[..., typer.testing.Result]
) -> typing.Callable[..., typer.testing.Result]:
	"""Wrap a CLI runner so that a plain ``init`` is copied in rather than run.

	Anything the store declines goes to ``invoke`` untouched, and so does any call expecting a
	failure or supplying input — those are asking about ``init`` rather than arranging one.
	"""

	def run (*arguments: str, expect: int = 0, input: str | None = None) -> typer.testing.Result:
		"""Run one command, or plant the instance it would have built."""

		if expect == 0 and input is None:
			recorded = store.planted(tuple(arguments), invoke)

			if recorded is not None:
				return recorded

		return invoke(*arguments, expect=expect, input=input)

	return run


def _destinations () -> dict[str, pathlib.Path]:
	"""Where the environment currently says an instance's files belong."""

	found = {name: os.environ.get(name) for name in VARIABLES}

	if any(value is None for value in found.values()):
		return {}

	return {name: pathlib.Path(value) for name, value in found.items() if value is not None}


def _occupied (where: dict[str, pathlib.Path]) -> bool:
	"""Whether anything has already been written where an instance would go.

	**The reason a second ``init`` in one test still runs for real.** It does not refuse — it
	prints ``Already set up.`` and exits 0 — so replaying the first one's output here would
	quietly answer a different question than the one the test asked.
	"""

	return any(place.is_dir() and any(place.iterdir()) for place in where.values())


@contextlib.contextmanager
def _aimed_at (sources: dict[str, pathlib.Path]) -> typing.Iterator[None]:
	"""Point the XDG variables at a template while it is built, and put them back after.

	``os.environ`` directly rather than ``monkeypatch``, because this runs *inside* a test whose
	own environment is already patched and has to survive: what is borrowed here is three
	variables for the length of one ``init``.
	"""

	before = {name: os.environ.get(name) for name in sources}

	for name, where in sources.items():
		os.environ[name] = str(where)

	try:
		yield

	finally:
		for name, value in before.items():
			if value is None:
				os.environ.pop(name, None)

			else:
				os.environ[name] = value
