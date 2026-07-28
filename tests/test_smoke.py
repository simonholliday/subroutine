"""Smoke tests confirming the package imports and reports a sane version."""

import subroutine


def test_package_imports () -> None:
	"""The package can be imported and exposes a version string."""

	assert isinstance(subroutine.__version__, str)
	assert subroutine.__version__


def test_api_version_is_pinned () -> None:
	"""The wire API version is declared, and is distinct from the package version."""

	assert subroutine.API_VERSION == "1.0"
