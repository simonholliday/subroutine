"""`--hide-nothing` beside an empty `--hide-status` or `--hide-document-status` is refused - `SR#5243`.

Found building `SR#5147`, and placed in 1.0 in `#5097`. ``''`` asks for a list to go back to
inheriting what is above it, and `--hide-nothing` for everything to be offered: the two say opposite
things, and the command took the second and dropped the first without a word.
"""

import json
import typing

import pytest
import typer.testing

import test_personal_path

#: The personal path's fresh home and its runner, bound here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run


@pytest.mark.parametrize("flag", ["--hide-status", "--hide-document-status"])
def test_hide_nothing_beside_an_empty_list_is_refused_and_changes_nothing (
	run: typing.Callable[..., typer.testing.Result], flag: str
) -> None:
	"""Refused with the sentence that refuses a key there, and the project is left as it was.

	The project hides one of each kind first, so a command that went ahead anyway - either reading
	of it - would show in what the listing reports afterwards.
	"""

	run("init")
	run("project", "create", "web", "Web")
	run("project", "update", "web", "--hide-status", "blocked", "--hide-document-status", "draft")

	refused = run("project", "update", "web", "--hide-nothing", flag, "", expect=1)

	assert f"{flag} and --hide-nothing say opposite things." in refused.output, refused.output

	rows = json.loads(run("project", "list", "--json").output)
	web = next(one for one in rows if one["key"] == "web")

	assert (web["hidden_statuses"], web["hidden_document_statuses"]) == (["blocked"], ["draft"]), (
		"a refused command changed the project anyway"
	)
