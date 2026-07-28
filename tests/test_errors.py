"""Tests for the error contract.

The registry is published and covered by semantic versioning, so most of these are
guards on a *contract* rather than on behaviour: that a code cannot be quietly renamed
into a different shape, that the documented registry matches the enforced one, and that
the class reporting a failure cannot disagree with the status its code claims.
"""

import pathlib

import pytest

import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.errors

DOCUMENTED_REGISTRY = pathlib.Path(__file__).resolve().parent.parent / "docs" / "errors.md"


def test_every_code_is_a_usable_contract_value () -> None:
	"""Codes are compared by machines, so their shape is part of the promise."""

	for code, entry in subroutine.errors.REGISTRY.items():
		assert code == entry.code, "registry keys and entries must agree"
		assert code.islower()
		assert code.replace("_", "").isalnum(), f"{code} is not snake_case"
		assert 400 <= entry.status <= 599
		assert entry.title and entry.title[0].isupper()
		assert entry.description.endswith(".")


def test_the_type_uri_identifies_one_code () -> None:
	"""One URI per code, so it can resolve to the entry describing it."""

	entry = subroutine.errors.definition("invalid_field_value")

	assert entry.type_uri == "https://subroutine.dev/errors/invalid-field-value"

	uris = {entry.type_uri for entry in subroutine.errors.REGISTRY.values()}

	assert len(uris) == len(subroutine.errors.REGISTRY)


def test_an_unregistered_code_names_the_registered_ones () -> None:
	"""The error about an error is held to the same standard as the rest."""

	with pytest.raises(ValueError) as error:
		subroutine.errors.definition("invented_on_the_spot")

	assert "invented_on_the_spot" in str(error.value)
	assert "not_found" in str(error.value)


@pytest.mark.parametrize(
	("exception", "status"),
	[
		(subroutine.errors.BadRequest, 400),
		(subroutine.errors.Unauthenticated, 401),
		(subroutine.errors.Forbidden, 403),
		(subroutine.errors.NotFound, 404),
		(subroutine.errors.Conflict, 409),
		(subroutine.errors.PayloadTooLarge, 413),
		(subroutine.errors.ValidationError, 422),
		(subroutine.errors.RateLimited, 429),
		(subroutine.errors.InternalError, 500),
	],
)
def test_each_exception_reports_its_own_status (
	exception: type[subroutine.errors.SubroutineError], status: int
) -> None:
	"""The class is the status; the code is which failure within it."""

	assert exception("Something went wrong.").status == status


def test_a_code_from_another_status_is_refused_at_construction () -> None:
	"""A 404 document claiming to be a 409 should fail where the mistake is, not later."""

	# Same status, different reason: fine.
	assert subroutine.errors.Conflict("Clash.", code="cycle_detected").status == 409

	with pytest.raises(ValueError) as error:
		subroutine.errors.NotFound("Missing.", code="version_conflict")

	assert "NotFound" in str(error.value)
	assert "409" in str(error.value)


def test_a_problem_document_has_the_rfc_9457_shape () -> None:
	"""SPEC.md §8.8."""

	error = subroutine.errors.ValidationError(
		"Unknown status key 'in-progress' for entity type 'task'.",
		code="invalid_status",
		errors=[
			subroutine.errors.FieldError(
				field="status",
				code="not_found",
				message="No status with key 'in-progress' exists in this workspace.",
				hint="Valid keys: open, in_progress, blocked, done, cancelled.",
			)
		],
	)

	document = subroutine.errors.problem_document(
		error, instance="/v1/tasks", request_id="01J8X"
	)

	assert document["type"] == "https://subroutine.dev/errors/invalid-status"
	assert document["title"] == "Invalid status"
	assert document["status"] == 422
	assert document["code"] == "invalid_status"
	assert document["instance"] == "/v1/tasks"
	assert document["request_id"] == "01J8X"
	assert document["errors"] == [
		{
			"field": "status",
			"code": "not_found",
			"message": "No status with key 'in-progress' exists in this workspace.",
			"hint": "Valid keys: open, in_progress, blocked, done, cancelled.",
		}
	]


def test_optional_members_are_absent_rather_than_null () -> None:
	"""A caller checking ``"hint" in document`` should not be misled by a null."""

	document = subroutine.errors.problem_document(subroutine.errors.NotFound("Gone."))

	assert set(document) == {"type", "title", "status", "detail", "code"}


def test_a_field_error_without_a_hint_omits_it () -> None:
	"""Same rule one level down."""

	rendered = subroutine.errors.FieldError(
		field="title", code="missing_field", message="A title is required."
	).as_dict()

	assert "hint" not in rendered


def test_the_published_registry_matches_the_enforced_one () -> None:
	"""``docs/errors.md`` is generated; a hand-edit or a forgotten regeneration fails here."""

	assert DOCUMENTED_REGISTRY.is_file(), f"{DOCUMENTED_REGISTRY} is missing"

	assert DOCUMENTED_REGISTRY.read_text(encoding="utf-8") == subroutine.errors.registry_markdown(), (
		"docs/errors.md is out of date — regenerate it from subroutine.errors"
	)


def test_an_authentication_failure_says_nothing_about_which_one_it_was () -> None:
	"""Every reason reports identically, so half a guessed credential stays unconfirmed."""

	reasons = list(subroutine.domain.authentication.AuthenticationFailure)
	details = set()

	for reason in reasons:
		error = subroutine.domain.authentication.AuthenticationError(reason, prefix="0123abcd")

		assert error.status == 401
		assert error.code == "unauthenticated"

		details.add(error.detail)

	# Invariance is the property, not silence. The message lists every reason it could
	# have been, which helps the holder of the token and distinguishes nothing for anyone
	# else; what would leak is the message *changing* with the reason.
	assert len(reasons) > 1
	assert len(details) == 1, "the message must not vary with the reason"


def test_an_authorization_failure_names_the_permission_it_needed () -> None:
	"""A refusal an agent can act on names what would have worked."""

	error = subroutine.domain.authorization.AuthorizationError(
		subroutine.domain.authorization.AuthorizationFailure.OUT_OF_TOKEN_SCOPE,
		permission="task:write",
		workspace_id=subroutine.db.types.new_uuid(),
	)

	assert error.status == 403
	assert "task:write" in error.detail
	assert error.hint is not None
	assert "task:write" in error.hint

	document = subroutine.errors.problem_document(error)

	assert document["hint"] == error.hint
