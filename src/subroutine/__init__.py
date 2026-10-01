"""Subroutine — a self-hosted, agent-native task and decision tracker for people and coding agents working on complex projects.

A self-hostable task and project tracker whose HTTP API, CLI and data model treat a
person and an AI agent as equally first-class users. See ``docs/design.md`` for the full
specification.
"""

import importlib.metadata


def _installed_version () -> str:
	"""Report the installed package version, or a placeholder when running from source."""

	try:
		return importlib.metadata.version("subroutine")

	except importlib.metadata.PackageNotFoundError:
		return "0.0.0+unknown"


__version__ = _installed_version()

#: **The contract the API's base path names** (decision `#4076`): ``"1.0"`` for as long as the
#: path is ``/v1``, and ``"2.0"`` only with a ``/v2``. It says nothing finer. Which additions an
#: instance has is ``instance_version``, the program's own number, so a release that adds a field
#: leaves this alone. Published in ``/v1/meta``, ``/v1/me``, ``/healthz``, ``/readyz``, the
#: ``X-Subroutine-Api-Version`` header, a client's ``User-Agent`` and the OpenAPI document, and
#: held to the paths the routers declare by ``tests/test_api_app.py``.
API_VERSION = "1.0"

#: Where to send a defect. Named here rather than beside the one caller because it is the
#: project's own address, not an instance's: ``config.source_url`` is what a *served*
#: instance declares about itself (§2.2) and an operator may point it at their own fork,
#: while a crash in this code belongs upstream wherever it was run.
ISSUES_URL = "https://github.com/simonholliday/subroutine/issues"
