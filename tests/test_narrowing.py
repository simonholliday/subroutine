"""One description of a credential on every surface - `SR#4564`, decision `#4527`.

**What it may do, then where, then until when, in ``token list``'s words**, said by
``views.narrowing`` for the terminal and the agent tools and by its twin, ``people.js``'s
``reachOf``, for the browser. There were five renderers in their own words (B section 1.3 of the
review of 2026-10-05): ``token create`` named the projects alone, ``token list`` left out a pin it
could not name, and the settings page said *within* and left out the pin altogether.
"""

import datetime
import json
import pathlib
import typing
import uuid

import subroutine.cli.main
import subroutine.views
import test_web

WEB = uuid.uuid4()
OPS = uuid.uuid4()
METACORTEX = uuid.uuid4()


def _token (**narrowing: typing.Any) -> subroutine.views.Token:
	"""Return a credential as ``GET /v1/tokens`` reports one, narrowed as asked."""

	fields: dict[str, typing.Any] = {
		"id": uuid.uuid4(),
		"title": "Deploys",
		"prefix": "a1b2c3d4",
		"user_id": uuid.uuid4(),
		"username": "claude",
		"scopes": [],
		"project_scope": None,
		"workspace_id": None,
		"narrows": False,
		"usable": True,
		"created_at": datetime.datetime(2026, 10, 7, tzinfo=datetime.UTC),
		"expires_at": None,
		"last_used_at": None,
		"revoked_at": None,
	}
	fields.update(narrowing)
	fields["narrows"] = bool(
		fields["scopes"]
		or fields["project_scope"] is not None
		or fields.get("project_write_scope") is not None
		or fields["workspace_id"] is not None
	)

	return subroutine.views.Token(**fields)


#: Every way a credential can be narrowed, each on its own and all at once, with and without a day.
CASES: dict[str, tuple[subroutine.views.Token, str | None]] = {
	"nothing narrows it": (_token(), None),
	"nothing narrows it, and it stops": (_token(), "2026-10-14"),
	"permissions": (_token(scopes=["task:read", "comment:write"]), None),
	"pinned, named": (_token(workspace_id=METACORTEX, workspace="metacortex"), None),
	"pinned, not named": (_token(workspace_id=METACORTEX), None),
	"projects by key": (
		_token(project_scope=[str(WEB), str(OPS)], project_scope_keys=["web", "ops"]), None
	),
	"projects by id": (_token(project_scope=[str(WEB)]), None),
	"projects with an empty list of keys": (
		_token(project_scope=[str(WEB)], project_scope_keys=[]), None
	),
	"no project at all": (_token(project_scope=[]), None),
	"writing in one": (
		_token(project_write_scope=[str(WEB)], project_write_scope_keys=["web"]), None
	),
	"writing nowhere": (_token(project_write_scope=[]), None),
	"all of it": (
		_token(
			scopes=["task:read", "task:write"],
			workspace_id=METACORTEX,
			workspace="metacortex",
			project_scope=[str(WEB), str(OPS)],
			project_scope_keys=["web", "ops"],
			project_write_scope=[str(WEB)],
			project_write_scope_keys=["web"],
		),
		"2026-10-14",
	),
}


def test_the_decided_words () -> None:
	"""Decision `#4527`'s own example, and the words for a credential nothing narrows."""

	every, day = CASES["all of it"]

	assert subroutine.views.narrowing(every, until=day) == (
		"task:read, task:write; in metacortex only; projects web, ops; writing only in web; "
		"until 2026-10-14"
	)
	assert subroutine.views.narrowing(CASES["nothing narrows it"][0]) == (
		"everything its owner can do"
	)
	assert subroutine.views.narrowing(CASES["permissions"][0]) == "comment:write, task:read", (
		"in one order, whatever order they were stored in"
	)
	assert subroutine.views.narrowing(CASES["pinned, not named"][0]) == (
		f"everything its owner can do; in {METACORTEX} only"
	)


def test_the_settings_page_says_it_in_the_same_words (tmp_path: pathlib.Path) -> None:
	"""``reachOf`` is ``views.narrowing``'s twin, word for word, across every case.

	The day is handed to both as a string, as each surface renders days its own way: a date in
	the reader's locale differs between this machine and CI (`#2252`), and that is not this test's
	subject.
	"""

	names = list(CASES)
	terminal = [subroutine.views.narrowing(CASES[name][0], until=CASES[name][1]) for name in names]
	answers = [
		[CASES[name][0].model_dump(mode="json"), CASES[name][1]] for name in names
	]
	browser = test_web._ran(tmp_path, f"""
		import * as app from "{test_web._staged(tmp_path).as_uri()}";

		const answers = {json.dumps(answers)};

		process.stdout.write(JSON.stringify(
			answers.map(([credential, until]) => app.reachOf(credential, until))
		));
	""")

	assert dict(zip(names, browser, strict=True)) == dict(zip(names, terminal, strict=True))


def test_a_listing_names_a_pin_it_cannot_name_by_its_id () -> None:
	"""``token list`` left out a pin to a workspace it could not name, so the credential read as
	reaching every workspace its owner does. It is named by id now, as ``whoami`` named one."""

	pinned, _day = CASES["pinned, not named"]
	line = subroutine.cli.main._credential_reach(
		pinned, {}, subroutine.cli.main.Reading("UTC", assumed=False)
	)

	assert f"in {METACORTEX} only" in line, line
	assert subroutine.cli.main._credential_reach(
		pinned, {METACORTEX: "metacortex"}, subroutine.cli.main.Reading("UTC", assumed=False)
	).startswith("everything its owner can do; in metacortex only · never used")
