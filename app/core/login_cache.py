"""Remember a successful password check for a few minutes (Pengu fork).

Why: one bcrypt check costs about half a second of CPU. A reseller's bot that
logs in before every request (hundreds of times a minute, each with the right
password) kept two of four cores busy with nothing but repeated hashing.

What is remembered: only a password that bcrypt itself has just accepted.

The rules this module keeps (do not relax them):

- A wrong password is never remembered and is always checked by bcrypt, so
  guessing over the network costs exactly what it did before.
- A remembered check can only repeat an answer bcrypt already gave, in this
  process, for the same password and the same stored hash. An entry is filed
  under the stored bcrypt hash, so changing the password (a new hash) stops
  the old password at once.
- Everything else about a login is unchanged: the admin is read from the
  database every time, and the disabled check still runs.
- The password itself is never stored. An entry is a PBKDF2-HMAC-SHA256 proof
  over (stored hash, password), keyed with a random key that lives only in
  this process's memory and is gone on restart. Two different passwords never
  share a proof (the password is in the message, not the HMAC key).
- An entry expires a fixed time after bcrypt accepted it (uses do not extend
  it), and expired entries are dropped from memory at the next login.

What it does cost, stated plainly: for the minutes an entry lives, someone who
can read this process's memory holds the key and the proof, and can test
password guesses at the proof's price (about 50 times cheaper than bcrypt on
the production host) instead of bcrypt's. That reader already has the token
signing key and the database password, and so the panel. The proof is slow on
purpose to keep even that gap small; do not replace it with a plain hash.

`PENGU_LOGIN_CACHE_SECONDS` (environment or `.env`) sets the window: default
300, at most 3600, and 0 switches the cache off. A value that cannot be read
switches it off too, with a warning, so a mistyped "off" never leaves it on.
"""

import asyncio
import hashlib
import hmac
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.utils.logger import get_logger

DEFAULT_SECONDS = 300
MAX_SECONDS = 3600
MAX_ENTRIES = 2048  # one entry per admin; far above any real number of admins
# About 9 ms on the production host, against about 470 ms for bcrypt (cost 12).
PROOF_ITERATIONS = 20_000

logger = get_logger("login-cache")


class _Settings(BaseSettings):
    """Read like the rest of the panel's settings: the environment first, then `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    seconds: str | None = Field(default=None, validation_alias="PENGU_LOGIN_CACHE_SECONDS")


def seconds_from_setting(raw: str | None) -> int:
    """The window in seconds. Unset means the default; unreadable means off."""
    if raw is None or not str(raw).strip():
        return DEFAULT_SECONDS
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning("PENGU_LOGIN_CACHE_SECONDS=%r is not a whole number; the login cache is off", raw)
        return 0
    return max(0, min(value, MAX_SECONDS))


class VerifiedLoginCache:
    def __init__(
        self,
        seconds: int = DEFAULT_SECONDS,
        *,
        max_entries: int = MAX_ENTRIES,
        iterations: int = PROOF_ITERATIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.seconds = max(0, min(int(seconds), MAX_SECONDS))
        self._max_entries = max(1, int(max_entries))
        self._iterations = max(1, int(iterations))
        self._clock = clock
        self._key = os.urandom(32)
        # stored bcrypt hash -> (proof, expires at); oldest first, which is also soonest to expire
        self._entries: OrderedDict[str, tuple[bytes, float]] = OrderedDict()

    def proof(self, raw: str, hashed: str) -> bytes | None:
        """The slow part (milliseconds). Call it off the event loop."""
        try:
            password = raw.encode("utf-8")
            stored = hashed.encode("utf-8")
        except UnicodeEncodeError, AttributeError:
            return None  # bcrypt refuses such input too; let it give the answer
        # The process key is the HMAC key and (hash, password) is the message. Never
        # the other way round: HMAC pads a short key with zero bytes and hashes a
        # long one, so with the password as the key "pw" and "pw\x00" would give
        # the same proof, and bcrypt tells those two apart.
        # The length prefix keeps (hash, password) pairs from running into each other.
        message = len(stored).to_bytes(4, "big") + stored + password
        return hashlib.pbkdf2_hmac("sha256", self._key, message, self._iterations)

    def _drop_expired(self) -> None:
        now = self._clock()
        while self._entries:
            hashed, (_, expires) = next(iter(self._entries.items()))
            if now < expires:
                break
            del self._entries[hashed]

    def has_entry(self, hashed: str) -> bool:
        """Is a proof worth computing: was a password accepted for this hash recently?"""
        if self.seconds <= 0:
            return False
        self._drop_expired()
        return hashed in self._entries

    def matches(self, hashed: str, proof: bytes | None) -> bool:
        """True only when bcrypt accepted this very password for this very hash, recently."""
        if self.seconds <= 0 or proof is None:
            return False
        self._drop_expired()
        entry = self._entries.get(hashed)
        return entry is not None and hmac.compare_digest(entry[0], proof)

    def remember(self, hashed: str, proof: bytes | None) -> None:
        """Call only after bcrypt has accepted the password this proof was made from."""
        if self.seconds <= 0 or proof is None:
            return
        self._drop_expired()
        self._entries.pop(hashed, None)
        self._entries[hashed] = (proof, self._clock() + self.seconds)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


_cache = VerifiedLoginCache(seconds_from_setting(_Settings().seconds))


async def verify_password_remembered(
    raw: str,
    hashed: str,
    verify: Callable[[str, str], Awaitable[bool]],
    *,
    cache: VerifiedLoginCache | None = None,
) -> bool:
    """`verify(raw, hashed)`, skipping the hashing when it has just accepted the same pair."""
    cache = _cache if cache is None else cache
    proof = None
    if cache.has_entry(hashed):
        proof = await asyncio.to_thread(cache.proof, raw, hashed)
        if cache.matches(hashed, proof):
            return True
    accepted = await verify(raw, hashed)
    if accepted is True and cache.seconds > 0:
        if proof is None:
            proof = await asyncio.to_thread(cache.proof, raw, hashed)
        cache.remember(hashed, proof)
    return accepted
