"""Published documentation that quotes the program must keep quoting it accurately.

``docs/hosting.md`` is the page somebody reads while deciding whether to trust this with a
server, and its most load-bearing part is the refusal ``serve`` gives when asked to listen
beyond the machine without TLS — quoted verbatim so the reader meets it here rather than at two
in the morning. A reworded refusal would leave the page confidently wrong, and nothing about
the page would look stale.

This is the same guard as ``tests/test_errors.py``'s and ``tests/test_api_examples.py``'s, and
for the same reason: prose that quotes code is only as true as the last time somebody checked.
Prose that merely *describes* code is deliberately not covered — a check that could not tell
"still accurate" from "reworded" would fail on every edit and be switched off.
"""

import pathlib
import typing

import pytest
import typer.testing

import subroutine.cli.main

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOSTING = ROOT / "docs" / "hosting.md"
README = ROOT / "README.md"


@pytest.fixture
def refusal () -> typing.Callable[..., str]:
	"""Return what ``serve`` says when it declines a bind, for a given configuration.

	No database is needed and none is made: the TLS check runs before ``serve`` looks for one,
	which is itself worth pinning — a refusal that only appeared on an initialised instance
	would arrive after the operator had already committed to the setup.
	"""

	runner = typer.testing.CliRunner()

	def asked (*arguments: str) -> str:
		"""Run one ``serve`` invocation that is expected to be refused."""

		result = runner.invoke(subroutine.cli.main.app, ["serve", *arguments])

		assert result.exit_code == 1, result.output

		return result.output

	return asked


def test_the_hosting_page_quotes_the_bind_refusal_as_it_is_actually_worded (
	refusal: typing.Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Both halves of it: with no ``public_url`` at all, and with one that is not https."""

	page = HOSTING.read_text(encoding="utf-8")

	for line in refusal("--host", "0.0.0.0").splitlines():
		assert line.strip() in page, f"docs/hosting.md no longer quotes: {line.strip()!r}"

	monkeypatch.setenv("SUBROUTINE_PUBLIC_URL", "http://tasks.example.com")

	for line in refusal("--host", "0.0.0.0").splitlines():
		assert line.strip() in page, f"docs/hosting.md no longer quotes: {line.strip()!r}"


@pytest.mark.parametrize("page", [HOSTING, README], ids=lambda path: path.name)
def test_published_pages_link_only_to_files_that_exist (page: pathlib.Path) -> None:
	"""A relative link in published documentation is a promise about the repository.

	Worth having because the README's links point *out* of its own directory: moving or
	renaming anything under ``docs/`` breaks them from a distance, and a dead link in the file
	GitHub renders first is read as a project that has stopped being maintained.
	"""

	text = page.read_text(encoding="utf-8")
	checked = 0

	for fragment in text.split("](")[1:]:
		target = fragment.split(")")[0].split("#")[0]

		if not target or target.startswith(("http://", "https://", "mailto:")):
			continue

		assert (page.parent / target).exists(), f"{page.name} links to a missing {target}"
		checked += 1

	# Otherwise this passes just as happily on a page whose links have all been deleted, which
	# is the failure mode a link checker is least able to notice about itself.
	assert checked, f"{page.name} has no relative links — has this test stopped reaching them?"
