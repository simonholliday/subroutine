"""Subroutine — project management for people and agents, in equal measure.

A self-hostable task and project tracker whose HTTP API, CLI and data model treat a
person and an AI agent as equally first-class users. See ``SPEC.md`` for the full
specification and ``MVP-PLAN.md`` for what is being built first.
"""

import importlib.metadata


def _installed_version () -> str:
	"""Report the installed package version, or a placeholder when running from source."""

	try:
		return importlib.metadata.version("subroutine")

	except importlib.metadata.PackageNotFoundError:
		return "0.0.0+unknown"


__version__ = _installed_version()

#: The API version exposed at ``/v1`` and reported in ``X-Subroutine-Api-Version``.
#: This tracks the wire contract, not the package release.
API_VERSION = "1.0"
