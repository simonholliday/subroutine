"""Tests for the credential primitives — hashing, minting, parsing.

No database and no configuration, matching the module under test. The cases that matter
are the ones where a plausible implementation is silently wrong: a token whose secret
contains the delimiter, and any route by which a secret reaches a string a program might log.
"""

import pytest

import subroutine.auth


def test_a_minted_token_parses_back_to_its_own_halves () -> None:
	"""What is handed to the user is what the parser expects to be given back."""

	issued = subroutine.auth.generate_token()
	parsed = subroutine.auth.parse_token(issued.value.get_secret_value())

	assert parsed is not None

	prefix, secret = parsed

	assert prefix == issued.prefix
	assert subroutine.auth.token_matches(secret, issued.token_hash)


def test_a_minted_token_has_the_documented_shape () -> None:
	"""docs/design.md §7.4: ``sr_<8 hex>_<secret>``."""

	value = subroutine.auth.generate_token().value.get_secret_value()
	scheme, prefix, secret = value.split("_", 2)

	assert scheme == "sr"
	assert len(prefix) == subroutine.auth.TOKEN_PREFIX_LENGTH
	assert int(prefix, 16) >= 0

	# 32 bytes base64url-encoded, so comfortably longer than this; the point is that it
	# carries real entropy rather than a counter.
	assert len(secret) >= 40


def test_a_secret_containing_underscores_survives_parsing () -> None:
	"""``token_urlsafe`` emits underscores, so the split must be bounded, not greedy."""

	parsed = subroutine.auth.parse_token("sr_0123abcd_aa_bb_cc")

	assert parsed == ("0123abcd", "aa_bb_cc")


def test_every_token_is_distinct () -> None:
	"""Two tokens minted in a row share neither prefix nor secret."""

	first = subroutine.auth.generate_token()
	second = subroutine.auth.generate_token()

	assert first.prefix != second.prefix
	assert first.token_hash != second.token_hash


@pytest.mark.parametrize(
	"presented",
	[
		"",
		"nonsense",
		"sr_0123abcd",
		"xx_0123abcd_secret",
		"sr_ABCDEF01_secret",
		"sr_0123abc_secret",
		"sr_0123abcde_secret",
		"sr_0123abcd_",
		"sr__secret",
	],
)
def test_malformed_tokens_are_refused_without_a_database (presented: str) -> None:
	"""Garbage is rejected on shape alone, before anything is looked up."""

	assert subroutine.auth.parse_token(presented) is None


def test_a_token_secret_never_reaches_a_string_a_program_might_log () -> None:
	"""docs/design.md §7.4: shown once, never recoverable, redacted in output and tracebacks."""

	issued = subroutine.auth.generate_token()
	secret = issued.value.get_secret_value().split("_", 2)[2]

	for rendering in (repr(issued), str(issued), f"{issued}", repr(issued.value), str(issued.value)):
		assert secret not in rendering

	# The public half stays legible: it is what a log line needs in order to name a token.
	assert issued.prefix in repr(issued)


def test_a_wrong_secret_does_not_match () -> None:
	"""The stored hash distinguishes the real secret from a near miss."""

	issued = subroutine.auth.generate_token()
	secret = issued.value.get_secret_value().split("_", 2)[2]

	assert subroutine.auth.token_matches(secret, issued.token_hash)
	assert not subroutine.auth.token_matches(secret + "x", issued.token_hash)
	assert not subroutine.auth.token_matches(secret[:-1], issued.token_hash)
