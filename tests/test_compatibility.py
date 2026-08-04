"""A client must be able to read a response from an instance older than itself — `#345`.

**The failure this exists for happened twice in one day**, on 2026-08-03, and the second time
was an hour after the first was understood and written down.

``/v1/me`` grew a client for the first time that morning (`#336`). Written as a subclass of
``views.User``, it inherited two fields the endpoint had never sent, and the new
``subroutine whoami`` refused the served instance with *"hpz2g4 answered, but not as a
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
"""

import typing

import pydantic
import pytest

import subroutine.views

#: ``GET /v1/me`` as served by 0.2.1.dev31 — the last shape before ``project_scope_keys``.
#: Captured 2026-08-03 from the instance on `hpz2g4`, which was a commit behind the tree.
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
