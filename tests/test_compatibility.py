"""A client must be able to read a response from an instance older than itself — `#345`.

**The failure this exists for happened twice in one day**, on 2026-08-03, and the second time
was an hour after the first was understood and written down.

``/v1/me`` grew a client for the first time that morning (`#336`). Written as a subclass of
``views.User``, it inherited two fields the endpoint had never sent, and the new
``subroutine whoami`` refused the served instance with *"workshop answered, but not as a
Subroutine instance"*. That was caught before it left the machine. The same afternoon, `#216`
added ``project_scope_keys`` to the same model as a **required** field — and this time it
reached a second machine, where `nuc14`'s freshly-installed CLI could not read the instance it
exists to talk to.

**Skew is the ordinary state of a fleet, not an edge case.** A server is upgraded on a
different day from the laptops that talk to it, and §13.7's whole design is machines reaching
one instance from wherever they are. `#89` will refuse a *mismatched* instance politely; that
is a different promise from this one, which is that a *compatible-enough* instance keeps
working.

**The rule this file enforces:** a field added to a response model after it has shipped must
carry a default, so that a body without it still parses. Nothing else in the suite could see
the problem — every test builds both halves from the same tree, which is exactly the
arrangement that cannot produce skew.

**The structural half of this rule lives in ``tests/test_response_compatibility.py``** (`#482`),
which diffs every view against the same view at the last tag. It exists because this file's rule
is general and its fixtures are one endpoint, and the difference let the same defect through a
third time on 2026-08-04. The two are not duplicates: that one asks *what changed since the
release*, this one holds a real body an older instance actually sent, which is the only thing
that can catch a field this build reads in a way no diff would notice.

The bodies below were **captured from a running instance one release behind** rather than
written by hand. That distinction is the point: a fixture written from the current models
agrees with them by construction and would have passed on both of the days above. Identifiers
are replaced with fixed ones; nothing about the shape is edited.

**Requests are the other half, and before 1.0 they are not promised** (`SR#2618`). Simon's answer
of 2026-09-14 was *reading only*, so an instance one release behind may refuse a parameter or a
route this build sends. What is held at the end of this file is the refusal's sentence, which
named a parameter the reader never typed (`SR#2625`), and those answers were captured from the
tag too.
"""

import inspect
import json
import typing

import httpx
import pydantic
import pytest

import subroutine.api.users
import subroutine.clients.http
import subroutine.connections
import subroutine.errors
import subroutine.installations
import subroutine.views

#: ``GET /v1/me`` as served by 0.2.1.dev31 — the last shape before ``project_scope_keys``.
#: Captured 2026-08-03 from a served instance that was a commit behind the tree.
ME_BEFORE_PROJECT_SCOPE_KEYS: dict[str, typing.Any] = {
	"api_version": "1.0",
	"user": {
		"id": "019fad98-4312-724d-b26c-24d9d0bc98b6",
		"username": "si",
		"display_name": None,
		"email": None,
		"timezone": "Etc/UTC",
		"is_superuser": True,
		"is_service_account": False,
	},
	"credential": {
		"kind": "api_token",
		"id": "019fb31d-c0aa-730b-8fc2-90a00e2eb732",
		"title": "A laptop",
		"prefix": "168d1187",
		"scopes": [],
		"project_scope": None,
		"workspace_id": None,
		"narrows": False,
		"expires_at": None,
		"last_used_at": "2026-08-03T08:35:33.475613Z",
	},
	"instance_permissions": [
		"instance:admin",
		"instance:user_create",
		"instance:workspace_create",
	],
	"workspaces": [
		{
			"id": "019fad98-4313-7e36-b972-f7decf66f8ae",
			"slug": "projects",
			"title": "Personal",
			"timezone": "Etc/UTC",
			"role": "superuser",
			"permissions": ["task:read", "task:write"],
			"narrowed_by_credential": False,
		}
	],
}

#: The same, from a credential that *is* restricted — the case that carries the new field, so
#: a reader can see that its absence is what is being tolerated rather than its emptiness.
ME_WITH_A_RESTRICTED_CREDENTIAL: dict[str, typing.Any] = {
	**ME_BEFORE_PROJECT_SCOPE_KEYS,
	"credential": {
		**ME_BEFORE_PROJECT_SCOPE_KEYS["credential"],
		"scopes": ["task:read"],
		"project_scope": ["019fc6b0-2e9d-7557-bce9-a497da0581da"],
		"narrows": True,
	},
}


@pytest.mark.parametrize(
	"body", [ME_BEFORE_PROJECT_SCOPE_KEYS, ME_WITH_A_RESTRICTED_CREDENTIAL]
)
def test_an_older_instances_identity_response_still_parses (
	body: dict[str, typing.Any],
) -> None:
	"""The exact body a released instance sends, read by this build's model."""

	answer = subroutine.views.Me.model_validate(body)

	assert answer.user.username == "si"
	assert answer.credential is not None
	assert answer.credential.title == "A laptop"


def test_a_field_the_older_instance_never_sent_reads_as_absent () -> None:
	"""And absent means *not stated*, never "restricted to no projects".

	The distinction matters because ``project_scope_keys`` is a convenience beside a field
	that has always been sent. A client wanting certainty reads ``project_scope``; anything
	rendering the keys falls back to it, so an older instance loses a nicety rather than
	reporting a credential as narrower than it is.
	"""

	answer = subroutine.views.Me.model_validate(ME_WITH_A_RESTRICTED_CREDENTIAL)

	assert answer.credential is not None
	assert answer.credential.project_scope_keys is None
	assert answer.credential.project_scope == ["019fc6b0-2e9d-7557-bce9-a497da0581da"]


def test_the_fixture_is_the_older_shape_rather_than_this_build_s () -> None:
	"""**The guard on the guard.** A fixture that quietly gained the field proves nothing.

	This is the failure mode the file's own docstring describes one level up: a compatibility
	test whose fixture is regenerated from the current models passes on the day the
	incompatibility ships. If somebody adds ``project_scope_keys`` to the captured body to
	"keep it current", this says so.
	"""

	assert "project_scope_keys" not in ME_BEFORE_PROJECT_SCOPE_KEYS["credential"]
	assert "project_scope_keys" in subroutine.views.Credential.model_fields


def test_a_missing_field_that_was_never_optional_is_still_refused () -> None:
	"""Tolerance is per field, not a switch. A body missing something real is still wrong.

	Without this, "be lenient about older servers" slides into "accept anything", and the
	refusal in ``clients/http._parsed`` — which exists so that a captive portal or a typo'd
	URL is reported rather than half-parsed — would stop firing.
	"""

	body = {
		**ME_BEFORE_PROJECT_SCOPE_KEYS,
		"user": {
			key: value
			for key, value in ME_BEFORE_PROJECT_SCOPE_KEYS["user"].items()
			if key != "username"
		},
	}

	with pytest.raises(pydantic.ValidationError):
		subroutine.views.Me.model_validate(body)


def test_an_instance_that_predates_the_version_fields_reads_as_saying_nothing () -> None:
	"""Item ``#381``'s two fields are the newest to be added to this response.

	They are the reason to be careful *here* of all places: a client that refused a body for
	lacking ``instance_version`` would refuse precisely the instances the field exists to
	identify — the older ones — turning a diagnostic into the failure it was meant to explain.

	Null means "did not say", and :func:`subroutine.views.versions` renders it as *"instance
	too old to say"* rather than as a blank, because that is itself the answer somebody
	looking for a missing feature has come for.
	"""

	answer = subroutine.views.Me.model_validate(ME_BEFORE_PROJECT_SCOPE_KEYS)

	assert answer.instance_version is None
	assert answer.schema_revision is None


def test_the_captured_bodies_predate_the_version_fields () -> None:
	"""The guard on the guard above, in the same shape as its neighbour.

	If somebody "keeps the fixture current" by adding the new keys, the test above starts
	asserting that this build's own output parses — which is true of every model and proves
	nothing about an older instance.
	"""

	for body in (ME_BEFORE_PROJECT_SCOPE_KEYS, ME_WITH_A_RESTRICTED_CREDENTIAL):
		assert "instance_version" not in body
		assert "schema_revision" not in body

	assert "instance_version" in subroutine.views.Me.model_fields
	assert "schema_revision" in subroutine.views.Me.model_fields


def _answering (body: dict[str, typing.Any]) -> subroutine.clients.http.Client:
	"""Return a client whose instance always answers with this body.

	`httpx.MockTransport` rather than the application, because the point is a body *this build
	cannot produce* — one an older instance sent. Building it from the app would regenerate it
	from the current models, which is the failure this whole file exists to prevent.
	"""

	return subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://work.example.com"),
		token="sr_x",
		transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
	)


def test_a_version_skewed_instance_is_reported_as_skewed_rather_than_as_not_an_instance () -> None:
	"""`#341`, which `#250` absorbed: the ordinary state was reported as a broken server.

	An instance one release behind answered `whoami`, and the client said *"workshop answered,
	but not as a Subroutine instance"* — about an instance it had been talking to all week —
	and pointed the reader at proxies and captive portals. §13.7 makes several connections
	normal and each may run a different release, so this is the fleet working as designed.

	**The body that fails to parse is the one that knows.** `instance_version` sits beside the
	field that is missing, so the explanation needs no second call and no earlier one.
	"""

	older = {
		**ME_BEFORE_PROJECT_SCOPE_KEYS,
		"instance_version": "0.4.0",
		"user": {
			key: value
			for key, value in ME_BEFORE_PROJECT_SCOPE_KEYS["user"].items()
			if key != "username"
		},
	}

	with (
		_answering(older) as client,
		pytest.raises(subroutine.errors.SubroutineError) as refused,
	):
		client.me()

	said = str(refused.value)

	assert "0.4.0" in said, "name what the instance is running"
	assert subroutine.installations.program() in said, "and what this program is"
	assert "not as a Subroutine instance" not in said, "it plainly is one"


def test_an_instance_that_names_no_version_is_still_reported_the_old_way () -> None:
	"""The half that must not be lost, and the reason the captured bodies are useful here.

	An instance predating `#381` sends no `instance_version`, so there is nothing to compare
	and nothing to say about versions. Guessing at that point would be worse than the original
	message: a proxy, a captive portal and a typo'd URL all answer like this too, and that is
	what the reader needs to hear when the instance itself has not identified itself.
	"""

	broken = {
		**ME_BEFORE_PROJECT_SCOPE_KEYS,
		"user": {
			key: value
			for key, value in ME_BEFORE_PROJECT_SCOPE_KEYS["user"].items()
			if key != "username"
		},
	}

	assert "instance_version" not in broken, "the captured body predates the field"

	with (
		_answering(broken) as client,
		pytest.raises(subroutine.errors.SubroutineError) as refused,
	):
		client.me()

	assert "not as a Subroutine instance" in str(refused.value)


def test_an_instance_running_this_build_is_not_reported_as_skewed () -> None:
	"""A body that fails to parse while the versions *agree* is not a version problem.

	This is the `#481` shape rather than a nicety: every development machine runs a build whose
	version string is fixed at whatever tag its last install saw, so a check that fired on any
	failure would blame skew on the one arrangement where there is none — an instance built
	from the same commit as its client.
	"""

	same = {
		**ME_BEFORE_PROJECT_SCOPE_KEYS,
		"instance_version": subroutine.installations.program(),
		"user": {
			key: value
			for key, value in ME_BEFORE_PROJECT_SCOPE_KEYS["user"].items()
			if key != "username"
		},
	}

	with (
		_answering(same) as client,
		pytest.raises(subroutine.errors.SubroutineError) as refused,
	):
		client.me()

	assert "not as a Subroutine instance" in str(refused.value)
	assert "disagree about what a response contains" not in str(refused.value)


#: ``GET /v1/me`` as ``v0.8.15`` answers it - `SR#2625`. Captured 2026-09-14 by driving the tag's
#: application in process, from a ``git archive`` of it, with every XDG directory empty.
#: Identifiers are replaced with fixed ones, and ``instance_version`` with the tag's own number:
#: run from an archive, a program reports the version installed beside it.
ME_AT_0_8_15: dict[str, typing.Any] = {
	"api_version": "1.0",
	"user": {
		"id": "01a0a15e-0000-7000-8000-000000000001",
		"username": "operator",
		"display_name": None,
		"email": None,
		"timezone": "Etc/UTC",
		"is_superuser": True,
		"is_service_account": False,
	},
	"instance_version": "0.8.15",
	"schema_revision": "1f61c97bf2ca",
	"credential": {
		"kind": "api_token",
		"id": "01a0a15e-0000-7000-8000-000000000002",
		"title": "capture",
		"prefix": "0815cafe",
		"scopes": [],
		"project_scope": None,
		"project_scope_keys": None,
		"project_write_scope": None,
		"project_write_scope_keys": None,
		"workspace_id": None,
		"narrows": False,
		"expires_at": None,
		"last_used_at": "2026-09-14T19:22:04.528871Z",
	},
	"instance_permissions": [
		"instance:admin",
		"instance:user_create",
		"instance:workspace_create",
	],
	"reader_timezone": "Etc/UTC",
	"workspaces": [
		{
			"id": "01a0a15e-0000-7000-8000-000000000003",
			"slug": "projects",
			"title": "Projects",
			"reader_timezone": "Etc/UTC",
			"prioritised_project": None,
			"timezone": "Etc/UTC",
			"role": "superuser",
			"permissions": [
				"comment:read",
				"comment:write",
				"link_type:write",
				"project:delete",
				"project:read",
				"project:write",
				"status:write",
				"tag:write",
				"task:delete",
				"task:read",
				"task:write",
				"token:admin",
				"user:admin",
				"workspace:admin",
				"workspace:delete",
				"workspace:read",
				"workspace:write",
			],
			"narrowed_by_credential": False,
		},
	],
}

#: What ``v0.8.15`` answered to requests this build sends, captured the same way and keyed by the
#: request exactly as this build's client makes it. That release's account listing declared no
#: query parameter and served no ``GET`` for one account, so the first three are refused by name.
#: The last two are refusals no release explains, held to show they keep the instance's words.
ANSWERED_AT_0_8_15: dict[tuple[str, str], tuple[int, dict[str, str], dict[str, typing.Any]]] = {
	("GET", "/v1/users?limit=50"): (
		422,
		{},
		{
			"type": "https://github.com/simonholliday/subroutine/blob/main/docs/errors.md#unknown_field",
			"title": "Unknown field",
			"status": 422,
			"detail": "This endpoint does not accept 'limit'.",
			"code": "unknown_field",
			"instance": "/v1/users",
			"request_id": "01a0a15e-0000-7000-8000-000000000811",
			"hint": "Refused rather than ignored, because a request that quietly ignores 'fields' returns the whole object and charges you for it.",
			"errors": [
				{
					"field": "limit",
					"code": "unknown_field",
					"message": "'limit' is not a parameter of this endpoint.",
					"hint": "It accepts: fields, format.",
				},
			],
		},
	),
	("GET", "/v1/users?limit=1000000&answers_to=operator"): (
		422,
		{},
		{
			"type": "https://github.com/simonholliday/subroutine/blob/main/docs/errors.md#unknown_field",
			"title": "Unknown field",
			"status": 422,
			"detail": "This endpoint does not accept 'answers_to'.",
			"code": "unknown_field",
			"instance": "/v1/users",
			"request_id": "01a0a15e-0000-7000-8000-000000000812",
			"hint": "Refused rather than ignored, because a request that quietly ignores 'fields' returns the whole object and charges you for it.",
			"errors": [
				{
					"field": "answers_to",
					"code": "unknown_field",
					"message": "'answers_to' is not a parameter of this endpoint.",
					"hint": "It accepts: fields, format.",
				},
				{
					"field": "limit",
					"code": "unknown_field",
					"message": "'limit' is not a parameter of this endpoint.",
					"hint": "It accepts: fields, format.",
				},
			],
		},
	),
	("GET", "/v1/users/operator"): (
		405,
		{
			"allow": "PATCH",
		},
		{
			"type": "https://github.com/simonholliday/subroutine/blob/main/docs/errors.md#method_not_allowed",
			"title": "Method not allowed",
			"status": 405,
			"detail": "GET is not accepted at /v1/users/operator.",
			"code": "method_not_allowed",
			"instance": "/v1/users/operator",
			"request_id": "01a0a15e-0000-7000-8000-000000000813",
			"hint": "This path accepts PATCH.",
		},
	),
	("PATCH", "/v1/workspaces/projects"): (
		422,
		{},
		{
			"type": "https://github.com/simonholliday/subroutine/blob/main/docs/errors.md#invalid_field_value",
			"title": "Invalid field value",
			"status": 422,
			"detail": "'appearence.colour' is not a setting a workspace has.",
			"code": "invalid_field_value",
			"instance": "/v1/workspaces/projects",
			"request_id": "01a0a15e-0000-7000-8000-000000000814",
			"errors": [
				{
					"field": "settings",
					"code": "unknown_field",
					"message": "Unknown setting 'appearence.colour'.",
					"hint": "A workspace accepts: appearance.colour, statuses.hidden.",
				},
			],
		},
	),
	("GET", "/v1/tasks/99999/comments"): (
		404,
		{},
		{
			"type": "https://github.com/simonholliday/subroutine/blob/main/docs/errors.md#not_found",
			"title": "Not found",
			"status": 404,
			"detail": "There is no task '99999' here.",
			"code": "not_found",
			"instance": "/v1/tasks/99999/comments",
			"request_id": "01a0a15e-0000-7000-8000-000000000815",
			"errors": [
				{
					"field": "id_or_ref",
					"code": "not_found",
					"message": "No task in projects answers to '99999'.",
					"hint": "Use a ref like '42' or a task id. GET /v1/tasks lists what you can see.",
				},
			],
		},
	),
}

#: The three commands `SR#2625` found refused by the release before, each as this build's client
#: asks it: ``user list`` with its default page, ``user deactivate`` asking whose agents it stops,
#: and ``user timezone`` reading one account.
ASKING_WHAT_0_8_15_LACKS: dict[
	str, tuple[tuple[str, str], typing.Callable[[subroutine.clients.http.Client], object]]
] = {
	"user list": (("GET", "/v1/users?limit=50"), lambda client: client.users(limit=50)),
	"user deactivate": (
		("GET", "/v1/users?limit=1000000&answers_to=operator"),
		lambda client: client.users(answers_to="operator", limit=1_000_000),
	),
	"user timezone": (
		("GET", "/v1/users/operator"),
		lambda client: client.user(username="operator"),
	),
}


def _at_0_8_15 (running: str) -> subroutine.clients.http.Client:
	"""Return a client whose instance answers as ``v0.8.15`` did, saying it runs ``running``."""

	def answer (request: httpx.Request) -> httpx.Response:
		"""Answer one request with what the tag answered to it."""

		if request.url.path == "/v1/me":
			return httpx.Response(200, json={**ME_AT_0_8_15, "instance_version": running})

		status, headers, body = ANSWERED_AT_0_8_15[(request.method, request.url.raw_path.decode())]

		return httpx.Response(
			status,
			headers={"content-type": "application/problem+json", **headers},
			content=json.dumps(body).encode(),
		)

	return subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://work.example.com"),
		token="sr_x",
		transport=httpx.MockTransport(answer),
	)


@pytest.mark.parametrize("command", sorted(ASKING_WHAT_0_8_15_LACKS))
def test_a_request_the_release_before_lacks_is_refused_naming_both_releases (command: str) -> None:
	"""`SR#2625`: *"This endpoint does not accept 'limit'."*, to somebody who typed `user list`.

	Before 1.0 the command need not work (`SR#2618`), so what is held is the sentence. It names
	what each side is running before anything else, keeps the instance's own words after that so
	nothing the instance said is lost, and keeps the code a program reads.
	"""

	request_made, asking = ASKING_WHAT_0_8_15_LACKS[command]
	answered = ANSWERED_AT_0_8_15[request_made][2]

	with _at_0_8_15("0.8.15") as client:
		client.me()

		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			asking(client)

	said = refused.value.detail

	assert said.startswith(
		f"work is running 0.8.15 and this program is {subroutine.installations.program()},"
	), said
	assert said.endswith(answered["detail"]), "the instance's own sentence, kept after the releases"
	assert refused.value.code == answered["code"], "and the code a program reads"
	assert refused.value.hint == subroutine.clients.http.UPDATE_THE_OLDER


@pytest.mark.parametrize(
	("request_made", "asking"),
	[
		(("GET", "/v1/tasks/99999/comments"), lambda client: client.comments(ref=99999)),
		(
			("PATCH", "/v1/workspaces/projects"),
			lambda client: client.update_workspace(
				"projects", settings={"appearence.colour": "dark"}
			),
		),
	],
	ids=["a missing item", "a mistyped setting"],
)
def test_a_refusal_no_release_explains_keeps_the_instance_s_words (
	request_made: tuple[str, str],
	asking: typing.Callable[[subroutine.clients.http.Client], object],
) -> None:
	"""A missing item and a mistyped setting are refused alike on every release.

	**``not_found`` is the one to hold.** A path with no route answers with it too, so naming the
	releases there would send somebody asking for an item that does not exist to go and upgrade.
	A setting key is typed by the reader, and its refusal is ``invalid_field_value``: a value is
	wrong, rather than the instance lacking something.
	"""

	with _at_0_8_15("0.8.15") as client:
		client.me()

		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			asking(client)

	assert refused.value.detail == ANSWERED_AT_0_8_15[request_made][2]["detail"]


def test_a_refusal_from_an_instance_on_this_release_keeps_its_own_words () -> None:
	"""The `#481` shape, on the request side: two builds of one release have no gap to name.

	A version string is compared rather than ranked, so the only case this can recognise as *no
	difference* is the same string - which is also the arrangement where a refusal is a real one.
	"""

	with _at_0_8_15(subroutine.installations.program()) as client:
		client.me()

		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			client.users(limit=50)

	assert refused.value.detail == "This endpoint does not accept 'limit'."


def test_a_refusal_before_the_instance_has_said_its_release_keeps_its_own_words () -> None:
	"""Nothing to compare, so nothing is guessed, which is the rule for a response too.

	The command line reads ``/v1/meta`` when it opens a connection, so it knows the release before
	any of these requests. A client that has asked nothing yet has only the refusal, and a refusal
	does not say which release wrote it.
	"""

	with (
		_at_0_8_15("0.8.15") as client,
		pytest.raises(subroutine.errors.SubroutineError) as refused,
	):
		client.users(limit=50)

	assert refused.value.detail == "This endpoint does not accept 'limit'."


def test_the_captured_refusals_are_of_a_release_before_this_build () -> None:
	"""The guard on the guard: a capture regenerated from this build would refuse none of it.

	This build's listing declares both names the tag refused and serves the ``GET`` it refused,
	while the tag's own refusal says what it accepted instead.
	"""

	declared = inspect.signature(subroutine.api.users.listing).parameters
	served = {
		(getattr(route, "path", None), method)
		for route in subroutine.api.users.router.routes
		for method in getattr(route, "methods", None) or ()
	}
	refused = ANSWERED_AT_0_8_15[("GET", "/v1/users?limit=50")][2]

	assert {"limit", "answers_to"} <= set(declared)
	assert ("/v1/users/{username}", "GET") in served
	assert refused["errors"][0]["hint"] == "It accepts: fields, format."
