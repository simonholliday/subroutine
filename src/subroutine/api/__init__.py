"""The HTTP API.

Thin by design. Every rule about what may happen lives in ``subroutine.domain``, so that
the CLI and the API cannot come to different conclusions about the same request — the
routers translate between HTTP and the service layer and do nothing else (SPEC.md §8.1).

The application is built by a factory rather than created at import time. A module-level
application would open the user's real database as a side effect of importing this
package, which makes it impossible to test against a temporary one and surprising to
import for any other reason.
"""
