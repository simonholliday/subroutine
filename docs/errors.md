# Error codes

Every error response carries a `code`. These are part of the public contract and
covered by semantic versioning: branch on them freely. Adding a code is a minor
version; renaming or removing one is a major version.

The `type` URI of a problem document links to this page's entry for that code, so
following one lands on the section describing it.

<!-- Generated from subroutine/errors.py. Do not edit by hand. -->

| Code | HTTP | Title | Meaning |
| --- | --- | --- | --- |
| `cycle_detected` | 409 | Cycle detected | The change would make something its own ancestor, in a project tree, a task hierarchy or a chain of blocking links. |
| `duplicate_key` | 409 | Already exists | Something with that identifying value is already here — a project key, a username, a tag name. |
| `forbidden` | 403 | Not permitted | The credential is valid but does not carry the permission this action needs. The permission is named, so a caller can ask for a token that has it. |
| `internal_error` | 500 | Internal error | Something failed that should not have. The detail is deliberately vague; the request id is what ties the response to the log entry that explains it. |
| `invalid_field_value` | 422 | Invalid field value | The request was well-formed but a field's value cannot be used. The field is named and, where the valid values are a known set, they are listed. |
| `invalid_status` | 422 | Invalid status | No status with that key exists for this entity type in this workspace. The valid keys are listed; an installation may rename them freely, so they are read from the workspace rather than assumed. |
| `malformed_request` | 400 | Malformed request | The request could not be read at all — bad JSON, a broken header, or a parameter that is not of the shape the endpoint accepts. |
| `method_not_allowed` | 405 | Method not allowed | The path exists but does not answer to that HTTP method. The methods it does answer to are listed in the 'Allow' header. |
| `missing_field` | 422 | Missing field | A field this endpoint requires was not supplied. |
| `not_found` | 404 | Not found | There is no such thing, or it is not visible to this caller. The two are deliberately not distinguished: saying 'forbidden' about a private project would confirm it exists (SPEC.md §7.3a). |
| `payload_too_large` | 413 | Too large | A field or the request body exceeds the configured limit. The limit is reported rather than the value being silently truncated (SPEC.md §6.10). |
| `rate_limited` | 429 | Too many requests | The caller is going faster than the configured limit allows. The response says when to try again. |
| `schema_mismatch` | 409 | Schema mismatch | A database schema does not match the one this build expects — either a backup being put back (SPEC.md §12.6) or the live database itself (§12.4a). An older schema can be migrated forward, and the refusal says so; a *newer* one cannot, because this version cannot interpret data it does not know the shape of and a partial read is worse than a clear failure. |
| `service_unavailable` | 503 | Not ready | The instance is running but cannot serve requests yet — most often its database is unreachable, or its schema has not been brought up to date. Reported by the readiness check so that a deployment holds traffic back rather than serving errors. |
| `unauthenticated` | 401 | Not authenticated | No credential was presented, or the one presented is not valid. Every reason reports identically: an unknown token, a revoked one and an expired one are indistinguishable from outside on purpose. |
| `unknown_field` | 422 | Unknown field | The request body carried a field this endpoint does not accept. Rejected rather than ignored, because silently dropping a typo is how a caller comes to believe it set something it did not (SPEC.md §8.1). |
| `version_conflict` | 409 | Version conflict | The entity changed since the version the caller sent. The response carries both versions and the current entity, so the caller can merge rather than refetch and start again. |

## cycle_detected

**Cycle detected** — HTTP 409.

The change would make something its own ancestor, in a project tree, a task hierarchy or a chain of blocking links.

## duplicate_key

**Already exists** — HTTP 409.

Something with that identifying value is already here — a project key, a username, a tag name.

## forbidden

**Not permitted** — HTTP 403.

The credential is valid but does not carry the permission this action needs. The permission is named, so a caller can ask for a token that has it.

## internal_error

**Internal error** — HTTP 500.

Something failed that should not have. The detail is deliberately vague; the request id is what ties the response to the log entry that explains it.

## invalid_field_value

**Invalid field value** — HTTP 422.

The request was well-formed but a field's value cannot be used. The field is named and, where the valid values are a known set, they are listed.

## invalid_status

**Invalid status** — HTTP 422.

No status with that key exists for this entity type in this workspace. The valid keys are listed; an installation may rename them freely, so they are read from the workspace rather than assumed.

## malformed_request

**Malformed request** — HTTP 400.

The request could not be read at all — bad JSON, a broken header, or a parameter that is not of the shape the endpoint accepts.

## method_not_allowed

**Method not allowed** — HTTP 405.

The path exists but does not answer to that HTTP method. The methods it does answer to are listed in the 'Allow' header.

## missing_field

**Missing field** — HTTP 422.

A field this endpoint requires was not supplied.

## not_found

**Not found** — HTTP 404.

There is no such thing, or it is not visible to this caller. The two are deliberately not distinguished: saying 'forbidden' about a private project would confirm it exists (SPEC.md §7.3a).

## payload_too_large

**Too large** — HTTP 413.

A field or the request body exceeds the configured limit. The limit is reported rather than the value being silently truncated (SPEC.md §6.10).

## rate_limited

**Too many requests** — HTTP 429.

The caller is going faster than the configured limit allows. The response says when to try again.

## schema_mismatch

**Schema mismatch** — HTTP 409.

A database schema does not match the one this build expects — either a backup being put back (SPEC.md §12.6) or the live database itself (§12.4a). An older schema can be migrated forward, and the refusal says so; a *newer* one cannot, because this version cannot interpret data it does not know the shape of and a partial read is worse than a clear failure.

## service_unavailable

**Not ready** — HTTP 503.

The instance is running but cannot serve requests yet — most often its database is unreachable, or its schema has not been brought up to date. Reported by the readiness check so that a deployment holds traffic back rather than serving errors.

## unauthenticated

**Not authenticated** — HTTP 401.

No credential was presented, or the one presented is not valid. Every reason reports identically: an unknown token, a revoked one and an expired one are indistinguishable from outside on purpose.

## unknown_field

**Unknown field** — HTTP 422.

The request body carried a field this endpoint does not accept. Rejected rather than ignored, because silently dropping a typo is how a caller comes to believe it set something it did not (SPEC.md §8.1).

## version_conflict

**Version conflict** — HTTP 409.

The entity changed since the version the caller sent. The response carries both versions and the current entity, so the caller can merge rather than refetch and start again.
