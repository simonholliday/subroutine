"""Turning credentials into stored values, and back into a yes or a no.

Pure functions only: nothing here reads configuration or touches a database, so every
rule can be tested directly and ``subroutine init`` can hash a password before either
exists. The lifecycle around these — issuing, revoking, checking a token against a live
row — is ``subroutine.domain.authentication``.

Two different hashes, for two different reasons (docs/design.md §7.4, §7.6):

* **Passwords** are hashed with Argon2id, slowly and with a per-user salt, because people
  choose passwords from a space small enough to search.
* **Token secrets** are hashed with SHA-256, quickly, because they are 256 bits of
  ``secrets`` output and the space is not searchable at any speed. Argon2 on the token
  path would put ~100 ms on every authenticated request to defend against nothing.
"""

import dataclasses
import hashlib
import hmac
import re
import secrets

import argon2
import argon2.exceptions
import pydantic

#: Identifies a Subroutine token at a glance, in a log or a leaked config file — the same
#: reason GitHub and Stripe prefix theirs.
TOKEN_SCHEME = "sr"

#: Hex characters in the public half of a token. Long enough that collisions are not a
#: practical concern, short enough to quote in a log line.
TOKEN_PREFIX_LENGTH = 8

#: The word between the scheme and the prefix that says what *kind* of credential this is.
#: An API token has none, so every credential issued before these existed is unchanged and
#: the commonest case stays the shortest.
#:
#: They exist so that a credential presented in the wrong place can be **refused by name**
#: rather than reported as a mistyped token — the pattern §7.4 already uses for a calendar
#: feed's ``sr_cal_``. A person holding a browser session and a terminal cannot tell two
#: opaque strings apart, so the program has to.
SESSION_KIND = "web"
LOGIN_KIND = "lnk"

#: Every kind this program mints, for the refusals that have to name them. A calendar
#: credential (§20.2) is *not* here: it is refused by prefix and nothing issues one yet.
CREDENTIAL_KINDS = (SESSION_KIND, LOGIN_KIND)

#: Bytes of entropy in the secret half. 256 bits is what makes a fast hash correct.
TOKEN_SECRET_BYTES = 32

#: docs/design.md §7.6. Long enough to matter, with no composition rules — those demonstrably
#: reduce entropy by pushing everyone towards the same handful of substitutions.
MINIMUM_PASSWORD_LENGTH = 12

#: A floor, not a filter. Catching the passwords that appear in every breach corpus is
#: worth the twenty lines; pretending this is a serious denylist would not be.
COMMON_PASSWORDS = frozenset(
	{
		"123456789012",
		"111111111111",
		"password1234",
		"passwordpassword",
		"qwertyuiop12",
		"administrator",
		"letmeinplease",
		"iloveyou1234",
		"welcome12345",
		"monkeymonkey",
		"dragondragon",
		"trustno1trustno1",
		"changeme1234",
		"secretsecret",
		"subroutine12",
	}
)

_PREFIX_PATTERN = re.compile(rf"^[0-9a-f]{{{TOKEN_PREFIX_LENGTH}}}$")

#: Argon2id at the library's defaults, which track current guidance. Parameters live in
#: the hash string itself, so raising them later is a rehash on next login, not a
#: migration.
_HASHER = argon2.PasswordHasher()


@dataclasses.dataclass(frozen=True, repr=False)
class IssuedToken:
	"""A newly minted token: the one moment its secret exists in readable form.

	``value`` is wrapped so that printing this object, interpolating it into a log line or
	letting it surface in a traceback cannot disclose the secret. Reading it takes
	``.get_secret_value()``, which is deliberately something a person has to type.
	"""

	value: pydantic.SecretStr
	prefix: str
	token_hash: str

	def __repr__ (self) -> str:
		"""Describe the token by its public half only."""

		return f"IssuedToken(prefix={self.prefix!r})"


def generate_token (*, kind: str | None = None) -> IssuedToken:
	"""Mint a credential as ``sr_<prefix>_<secret>``, or ``sr_<kind>_<prefix>_<secret>``.

	The prefix is stored in the clear and indexed, so authenticating is one row fetch
	rather than a scan of every token comparing hashes.

	``kind`` says what the credential is for, and every kind shares this one implementation
	deliberately: a browser session with its own minting code would be a second copy of a
	grammar that has to agree with :func:`parse_token` to be safe at all.
	"""

	prefix = secrets.token_hex(TOKEN_PREFIX_LENGTH // 2)
	secret = secrets.token_urlsafe(TOKEN_SECRET_BYTES)
	marked = TOKEN_SCHEME if kind is None else f"{TOKEN_SCHEME}_{kind}"

	return IssuedToken(
		value=pydantic.SecretStr(f"{marked}_{prefix}_{secret}"),
		prefix=prefix,
		token_hash=hash_token_secret(secret),
	)


def parse_token (presented: str, *, kind: str | None = None) -> tuple[str, str] | None:
	"""Split a presented credential into its prefix and secret, or ``None`` if malformed.

	Rejecting an ill-formed token here saves a pointless database round trip on every
	piece of garbage sent to the API, and gives the caller a reason it can log safely.

	**A credential of one kind never parses as another.** Asked for an API token, a browser
	session's ``sr_web_…`` returns ``None`` rather than being looked up in the wrong table —
	which is what lets the caller refuse it by name instead of reporting it as mistyped.
	"""

	wanted = TOKEN_SCHEME if kind is None else f"{TOKEN_SCHEME}_{kind}"
	parts = presented.strip().split("_", 2 if kind is None else 3)

	if len(parts) != (3 if kind is None else 4):
		return None

	*scheme, prefix, secret = parts

	# The secret half comes from `token_urlsafe` and may itself contain underscores, which
	# is why the split above is bounded rather than greedy.
	if "_".join(scheme) != wanted or not secret or not _PREFIX_PATTERN.match(prefix):
		return None

	return prefix, secret


def hash_token_secret (secret: str) -> str:
	"""Return the stored form of a token secret.

	Unpeppered, deliberately. A pepper defends a hash whose input can be guessed; this
	input is 256 random bits, so it adds nothing — while tying every issued token to the
	lifetime of a configuration value, so that rotating the signing key would lock out
	every agent in the installation at once (docs/design.md §7.4).
	"""

	return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def token_matches (secret: str, stored_hash: str) -> bool:
	"""Report whether a presented secret hashes to the stored value."""

	return hmac.compare_digest(hash_token_secret(secret), stored_hash)


def hash_password (password: str) -> str:
	"""Return the stored form of a password, salted and slow."""

	return _HASHER.hash(password)


def verify_password (stored_hash: str, password: str) -> bool:
	"""Report whether a password matches its stored hash.

	A malformed or truncated hash is treated as a failed login rather than an error: a
	corrupt row should refuse the user, not crash the request handling every user.
	"""

	try:
		return _HASHER.verify(stored_hash, password)

	except argon2.exceptions.VerificationError:
		return False

	except argon2.exceptions.InvalidHashError:
		return False


def password_needs_rehash (stored_hash: str) -> bool:
	"""Report whether a stored hash was made with parameters we have since raised.

	Checked on successful login, where the plaintext is in hand for the only moment it
	ever will be. Ignoring this means an installation's oldest accounts keep their
	weakest hashes forever.
	"""

	try:
		return _HASHER.check_needs_rehash(stored_hash)

	except argon2.exceptions.InvalidHashError:
		return True


def password_problem (password: str) -> str | None:
	"""Return what is wrong with a proposed password, or ``None`` if nothing is.

	Phrased for the person choosing it, since that is who reads it.
	"""

	if len(password) < MINIMUM_PASSWORD_LENGTH:
		return (
			f"Passwords need to be at least {MINIMUM_PASSWORD_LENGTH} characters, and that "
			f"one is {len(password)}. A short phrase you will remember works well."
		)

	if password.lower() in COMMON_PASSWORDS:
		return "That is one of the most commonly guessed passwords. Please choose another."

	return None
