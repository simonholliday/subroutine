# Error codes

Every error response carries a `code`. These are part of the public contract and
covered by semantic versioning: branch on them freely. Adding a code is a minor
version; renaming or removing one is a major version.

The `type` URI of a problem document links to this page's entry for that code, so
following one lands on the section describing it.

<!-- Generated from subroutine/errors.py. Do not edit by hand. -->

| Code | HTTP | Title | Meaning |
| --- | --- | --- | --- |
| `cursor_expired` | 410 | Cursor expired | A change-feed cursor names a point older than the events this instance still holds, so the gap between there and now cannot be reported (docs/design.md §5.11). The client resyncs from the beginning rather than being handed a page that silently omits everything pruned in between. |
| `cycle_detected` | 409 | Cycle detected | The change would make something its own ancestor, in a project tree, a task hierarchy or a chain of blocking links. |
| `duplicate_key` | 409 | Already exists | Something with that identifying value is already here — a project key, a username, a tag name. |
| `forbidden` | 403 | Not permitted | The credential is valid but does not permit this. Where the refusal turns on a permission the caller lacks, that permission is named so they can ask for a token carrying it — but several do not: a token pinned to another workspace, a caller who is not a member, and a project scope narrower than the project reached are each about reach rather than about a verb. |
| `internal_error` | 500 | Internal error | Something failed that should not have. The detail is deliberately vague; the request id is what ties the response to the log entry that explains it. |
| `invalid_field_value` | 422 | Invalid field value | The request was well-formed but a field's value cannot be used. The field is named and, where the valid values are a known set, they are listed. |
| `invalid_status` | 422 | Invalid status | The status asked for cannot be used here. Usually no status with that key exists for this entity type in this workspace, and then the valid keys are listed — an installation may rename them freely, so they are read from the workspace rather than assumed. It also reports a workspace with no default status at all, where there are no keys to list and the answer is to seed it. |
| `malformed_request` | 400 | Malformed request | The request could not be read at all — bad JSON, or a header this API has to parse and could not. A parameter of the wrong shape is a different answer: the request was read, so it is 422 'invalid_field_value' naming the parameter. |
| `method_not_allowed` | 405 | Method not allowed | The path exists but does not answer to that HTTP method. The methods it does answer to are listed in the 'Allow' header. |
| `missing_field` | 422 | Missing field | A field this endpoint requires was not supplied. |
| `not_found` | 404 | Not found | There is no such thing, or it is not visible to this caller. The two are deliberately not distinguished: saying 'forbidden' about a private project would confirm it exists (docs/design.md §7.3a). |
| `payload_too_large` | 413 | Too large | A field or the request body exceeds the configured limit. The limit is reported rather than the value being silently truncated (docs/design.md §6.10). |
| `rate_limited` | 429 | Too many requests | The caller is going faster than the configured limit allows. The response says when to try again. |
| `request_timed_out` | 503 | Timed out | The database work behind this request ran longer than 'request_timeout_seconds' allows, and was given up on. Distinct from 'service_unavailable', which says the instance cannot serve anything yet: this instance is serving, and it was this request that did not finish. The detail names what was being waited for where the database said, and retrying may work. |
| `schema_mismatch` | 409 | Schema mismatch | A database schema does not match the one this build expects, for a backup being put back (docs/design.md §12.6). An older schema can be migrated forward and the refusal says so; a *newer* one cannot, because this version cannot interpret data it does not know the shape of and a partial read is worse than a clear failure. The live database being out of step is a different answer (§12.4a): /readyz reports it as 503 service_unavailable, because a load balancer has to read this instance as not ready rather than as arguing. |
| `service_unavailable` | 503 | Not ready | The instance is running but cannot serve requests yet — most often its database is unreachable, or its schema has not been brought up to date. Reported by the readiness check so that a deployment holds traffic back rather than serving errors. |
| `unauthenticated` | 401 | Not authenticated | No credential was presented, or the one presented is not valid. Every reason reports identically: an unknown token, a revoked one and an expired one are indistinguishable from outside on purpose. |
| `unknown_field` | 422 | Unknown field | The request carried a field or query parameter this endpoint does not accept. Rejected rather than ignored, because silently dropping a typo is how a caller comes to believe it set something it did not (docs/design.md §8.1). |
| `unsupported_protocol_version` | 400 | Unsupported protocol version | A client announced an MCP revision this server does not speak, which the Streamable HTTP transport requires be refused rather than answered as though it were understood. The revision this server does speak is named, so a client can decide whether to continue. Distinct from 'malformed_request' because the request was read perfectly well. |
| `version_conflict` | 409 | Version conflict | The entity changed since the version the caller sent. The response carries both versions and the current entity, so the caller can merge rather than refetch and start again. |

## cursor_expired

**Cursor expired** — HTTP 410.

A change-feed cursor names a point older than the events this instance still holds, so the gap between there and now cannot be reported (docs/design.md §5.11). The client resyncs from the beginning rather than being handed a page that silently omits everything pruned in between.

## cycle_detected

**Cycle detected** — HTTP 409.

The change would make something its own ancestor, in a project tree, a task hierarchy or a chain of blocking links.

## duplicate_key

**Already exists** — HTTP 409.

Something with that identifying value is already here — a project key, a username, a tag name.

## forbidden

**Not permitted** — HTTP 403.

The credential is valid but does not permit this. Where the refusal turns on a permission the caller lacks, that permission is named so they can ask for a token carrying it — but several do not: a token pinned to another workspace, a caller who is not a member, and a project scope narrower than the project reached are each about reach rather than about a verb.

## internal_error

**Internal error** — HTTP 500.

Something failed that should not have. The detail is deliberately vague; the request id is what ties the response to the log entry that explains it.

## invalid_field_value

**Invalid field value** — HTTP 422.

The request was well-formed but a field's value cannot be used. The field is named and, where the valid values are a known set, they are listed.

## invalid_status

**Invalid status** — HTTP 422.

The status asked for cannot be used here. Usually no status with that key exists for this entity type in this workspace, and then the valid keys are listed — an installation may rename them freely, so they are read from the workspace rather than assumed. It also reports a workspace with no default status at all, where there are no keys to list and the answer is to seed it.

## malformed_request

**Malformed request** — HTTP 400.

The request could not be read at all — bad JSON, or a header this API has to parse and could not. A parameter of the wrong shape is a different answer: the request was read, so it is 422 'invalid_field_value' naming the parameter.

## method_not_allowed

**Method not allowed** — HTTP 405.

The path exists but does not answer to that HTTP method. The methods it does answer to are listed in the 'Allow' header.

## missing_field

**Missing field** — HTTP 422.

A field this endpoint requires was not supplied.

## not_found

**Not found** — HTTP 404.

There is no such thing, or it is not visible to this caller. The two are deliberately not distinguished: saying 'forbidden' about a private project would confirm it exists (docs/design.md §7.3a).

## payload_too_large

**Too large** — HTTP 413.

A field or the request body exceeds the configured limit. The limit is reported rather than the value being silently truncated (docs/design.md §6.10).

## rate_limited

**Too many requests** — HTTP 429.

The caller is going faster than the configured limit allows. The response says when to try again.

## request_timed_out

**Timed out** — HTTP 503.

The database work behind this request ran longer than 'request_timeout_seconds' allows, and was given up on. Distinct from 'service_unavailable', which says the instance cannot serve anything yet: this instance is serving, and it was this request that did not finish. The detail names what was being waited for where the database said, and retrying may work.

## schema_mismatch

**Schema mismatch** — HTTP 409.

A database schema does not match the one this build expects, for a backup being put back (docs/design.md §12.6). An older schema can be migrated forward and the refusal says so; a *newer* one cannot, because this version cannot interpret data it does not know the shape of and a partial read is worse than a clear failure. The live database being out of step is a different answer (§12.4a): /readyz reports it as 503 service_unavailable, because a load balancer has to read this instance as not ready rather than as arguing.

## service_unavailable

**Not ready** — HTTP 503.

The instance is running but cannot serve requests yet — most often its database is unreachable, or its schema has not been brought up to date. Reported by the readiness check so that a deployment holds traffic back rather than serving errors.

## unauthenticated

**Not authenticated** — HTTP 401.

No credential was presented, or the one presented is not valid. Every reason reports identically: an unknown token, a revoked one and an expired one are indistinguishable from outside on purpose.

## unknown_field

**Unknown field** — HTTP 422.

The request carried a field or query parameter this endpoint does not accept. Rejected rather than ignored, because silently dropping a typo is how a caller comes to believe it set something it did not (docs/design.md §8.1).

## unsupported_protocol_version

**Unsupported protocol version** — HTTP 400.

A client announced an MCP revision this server does not speak, which the Streamable HTTP transport requires be refused rather than answered as though it were understood. The revision this server does speak is named, so a client can decide whether to continue. Distinct from 'malformed_request' because the request was read perfectly well.

## version_conflict

**Version conflict** — HTTP 409.

The entity changed since the version the caller sent. The response carries both versions and the current entity, so the caller can merge rather than refetch and start again.
