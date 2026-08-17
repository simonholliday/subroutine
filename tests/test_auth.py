"""Tests for the credential primitives — hashing, minting, parsing.

No database and no configuration, matching the module under test. The cases that matter
are the ones where a plausible implementation is silently wrong: a token whose secret
contains the delimiter, a corrupt hash that raises instead of refusing, and any route by
which a secret reaches a string a program might log.
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


def test_passwords_hash_with_argon2id_and_a_fresh_salt () -> None:
	"""docs/design.md §7.6 names the variant; naming no algorithm is how MD5 happens."""

	first = subroutine.auth.hash_password("correct horse battery")
	second = subroutine.auth.hash_password("correct horse battery")

	assert first.startswith("$argon2id$")
	assert first != second, "identical passwords must not produce identical hashes"


def test_a_password_verifies_only_against_itself () -> None:
	"""The obvious property, and the one worth stating."""

	stored = subroutine.auth.hash_password("correct horse battery")

	assert subroutine.auth.verify_password(stored, "correct horse battery")
	assert not subroutine.auth.verify_password(stored, "correct horse batteries")
	assert not subroutine.auth.verify_password(stored, "")


def test_a_corrupt_hash_refuses_the_login_rather_than_raising () -> None:
	"""One damaged row should lock out one user, not crash every request."""

	assert not subroutine.auth.verify_password("not a hash at all", "anything")
	assert not subroutine.auth.verify_password("", "anything")


def test_a_current_hash_does_not_need_rehashing_and_a_broken_one_does () -> None:
	"""Rehash-on-login is what stops the oldest accounts keeping the weakest hashes."""

	stored = subroutine.auth.hash_password("correct horse battery")

	assert not subroutine.auth.password_needs_rehash(stored)
	assert subroutine.auth.password_needs_rehash("not a hash at all")


def test_short_and_common_passwords_are_refused_with_a_usable_reason () -> None:
	"""The message is read by whoever is choosing the password."""

	short = subroutine.auth.password_problem("abc")

	assert short is not None
	assert str(subroutine.auth.MINIMUM_PASSWORD_LENGTH) in short

	common = subroutine.auth.password_problem("password1234")

	assert common is not None
	assert "commonly guessed" in common

	assert subroutine.auth.password_problem("a decent enough passphrase") is None


def test_password_rules_do_not_impose_composition () -> None:
	"""docs/design.md §7.6: no character-class rules, because they reduce entropy in practice."""

	assert subroutine.auth.password_problem("all lower case letters only") is None
