"""Unit tests for app.core.login_cache (remembered password checks, Pengu fork).

Each test pins one of the rules written at the top of the module.
"""

import asyncio
import hashlib
import threading

import bcrypt

from app.core import login_cache
from app.core.login_cache import (
    DEFAULT_SECONDS,
    MAX_SECONDS,
    PROOF_ITERATIONS,
    VerifiedLoginCache,
    seconds_from_setting,
    verify_password_remembered,
)

HASH_A = "$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HASH_B = "$2b$12$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def new_cache(seconds: int = 300, **kwargs) -> VerifiedLoginCache:
    kwargs.setdefault("clock", Clock())
    kwargs.setdefault("iterations", 1)  # the cost of the proof is tested on its own
    return VerifiedLoginCache(seconds, **kwargs)


class Verifier:
    """Stands in for bcrypt: accepts the pairs it was given, and counts the calls."""

    def __init__(self, *accepted: tuple[str, str]) -> None:
        self.accepted = set(accepted)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, raw: str, hashed: str) -> bool:
        self.calls.append((raw, hashed))
        return (raw, hashed) in self.accepted


def check(cache, verifier, raw, hashed):
    return asyncio.run(verify_password_remembered(raw, hashed, verifier, cache=cache))


def test_an_accepted_password_is_hashed_once_per_window():
    cache, verifier = new_cache(), Verifier(("right", HASH_A))
    assert [check(cache, verifier, "right", HASH_A) for _ in range(50)] == [True] * 50
    assert verifier.calls == [("right", HASH_A)]


def test_a_wrong_password_is_never_remembered():
    cache, verifier = new_cache(), Verifier(("right", HASH_A))
    assert [check(cache, verifier, "wrong", HASH_A) for _ in range(5)] == [False] * 5
    assert len(verifier.calls) == 5, "every wrong guess must cost a full hash"
    assert len(cache) == 0


def test_a_wrong_password_is_refused_while_the_right_one_is_remembered():
    cache, verifier = new_cache(), Verifier(("right", HASH_A))
    assert check(cache, verifier, "right", HASH_A) is True
    for guess in ("wrong", "righ", "rightt", "Right", "", "right\x00"):
        assert check(cache, verifier, guess, HASH_A) is False, guess
    assert len(verifier.calls) == 7, "one accepted check plus one full hash per wrong guess"
    assert check(cache, verifier, "right", HASH_A) is True
    assert len(verifier.calls) == 7, "wrong guesses must not push the right password out"


def test_a_changed_password_is_not_accepted_from_memory():
    """Changing the password changes the stored hash; the old password must stop working at once."""
    cache = new_cache()
    verifier = Verifier(("old", HASH_A), ("new", HASH_B))
    assert check(cache, verifier, "old", HASH_A) is True
    # the admin's stored hash is now HASH_B
    assert check(cache, verifier, "old", HASH_B) is False
    assert check(cache, verifier, "new", HASH_B) is True
    assert verifier.calls == [("old", HASH_A), ("old", HASH_B), ("new", HASH_B)]


def test_the_same_password_on_another_admin_is_checked_separately():
    cache, verifier = new_cache(), Verifier(("shared", HASH_A))
    assert check(cache, verifier, "shared", HASH_A) is True
    assert check(cache, verifier, "shared", HASH_B) is False
    assert verifier.calls[-1] == ("shared", HASH_B)


def test_an_entry_expires_a_fixed_time_after_the_real_check():
    clock = Clock()
    cache, verifier = new_cache(clock=clock), Verifier(("right", HASH_A))
    assert check(cache, verifier, "right", HASH_A) is True
    for _ in range(10):  # heavy use must not keep the entry alive
        clock.now += 29.9
        assert check(cache, verifier, "right", HASH_A) is True
    assert len(verifier.calls) == 1
    clock.now += 1.1  # 300.1 s after the real check
    assert check(cache, verifier, "right", HASH_A) is True
    assert len(verifier.calls) == 2, "the hash must run again once the window is over"


def test_expired_entries_leave_memory_at_the_next_login():
    """An admin who never logs in again must not leave a proof behind until restart."""
    clock = Clock()
    cache = new_cache(clock=clock)
    verifier = Verifier(("one", HASH_A), ("two", HASH_B))
    assert check(cache, verifier, "one", HASH_A) is True
    clock.now += 200
    assert check(cache, verifier, "two", HASH_B) is True
    assert set(cache._entries) == {HASH_A, HASH_B}
    clock.now += 101  # HASH_A is 301 s old, HASH_B 101 s
    assert check(cache, verifier, "nobody", "$2b$12$unknown") is False  # any login at all
    assert set(cache._entries) == {HASH_B}
    clock.now += 200
    assert cache.has_entry(HASH_A) is False and len(cache) == 0


def test_zero_seconds_switches_the_cache_off():
    cache, verifier = new_cache(0), Verifier(("right", HASH_A))
    assert [check(cache, verifier, "right", HASH_A) for _ in range(3)] == [True] * 3
    assert len(verifier.calls) == 3 and len(cache) == 0


def test_the_password_is_not_kept_in_memory():
    cache, verifier = new_cache(), Verifier(("S3cret-Pass", HASH_A))
    assert check(cache, verifier, "S3cret-Pass", HASH_A) is True
    ((proof, _expires),) = cache._entries.values()
    assert isinstance(proof, bytes) and len(proof) == 32
    assert "S3cret-Pass" not in repr(vars(cache)) and "S3cret-Pass" not in repr(cache)


def test_the_proof_is_a_slow_salted_hash_not_a_plain_one():
    """What a reader of this process's memory would hold must stay expensive to guess against."""
    assert PROOF_ITERATIONS >= 20_000
    cache = VerifiedLoginCache(300, clock=Clock())
    assert cache._iterations == PROOF_ITERATIONS
    stored = HASH_A.encode()
    message = len(stored).to_bytes(4, "big") + stored + b"right"
    assert cache.proof("right", HASH_A) == hashlib.pbkdf2_hmac("sha256", cache._key, message, PROOF_ITERATIONS)
    assert len(cache._key) == 32


def test_no_two_passwords_share_a_proof():
    """HMAC treats a key and the same key plus zero bytes alike, and hashes a long key.

    So the password must be in the message. bcrypt tells all of these apart.
    """
    cache = VerifiedLoginCache(300, clock=Clock())  # the real iteration count
    long_password = "L" * 100
    variants = [
        "right",
        "right\x00",
        "right\x00\x00",
        "\x00right",
        "right ",
        "",
        "\x00",
        long_password,
        hashlib.sha256(long_password.encode()).digest().decode("latin-1"),
        "r" * 64,
        "r" * 65,
    ]
    proofs = [cache.proof(v, HASH_A) for v in variants]
    assert len(set(proofs)) == len(variants)
    verifier = Verifier(("right", HASH_A))
    assert check(cache, verifier, "right", HASH_A) is True
    for other in variants[1:]:
        assert check(cache, verifier, other, HASH_A) is False, repr(other)


def test_the_proof_is_computed_off_the_event_loop():
    cache, verifier = new_cache(), Verifier(("right", HASH_A))
    threads = []
    real = cache.proof

    def watched(raw, hashed):
        threads.append(threading.current_thread())
        return real(raw, hashed)

    cache.proof = watched
    assert check(cache, verifier, "right", HASH_A) is True
    assert check(cache, verifier, "right", HASH_A) is True
    assert len(threads) == 2 and all(t is not threading.main_thread() for t in threads)


def test_proofs_are_bound_to_this_process():
    """Another process (another worker, or the next start) cannot reuse an entry."""
    one, two = new_cache(), new_cache()
    assert one._key != two._key
    assert one.proof("right", HASH_A) != two.proof("right", HASH_A)
    two.remember(HASH_A, one.proof("right", HASH_A))
    assert two.matches(HASH_A, two.proof("right", HASH_A)) is False


def test_hash_and_password_cannot_be_mixed_up():
    cache = new_cache()
    assert cache.proof("bc", "a") != cache.proof("c", "ab")


def test_a_password_that_cannot_be_encoded_goes_to_the_real_check():
    cache, verifier = new_cache(), Verifier(("right", HASH_A))
    assert check(cache, verifier, "bad\ud800", HASH_A) is False
    assert len(verifier.calls) == 1 and len(cache) == 0
    assert check(cache, verifier, "right", HASH_A) is True
    assert check(cache, verifier, "bad\ud800", HASH_A) is False  # with a live entry too
    assert len(verifier.calls) == 3
    assert cache.matches(HASH_A, None) is False


def test_only_a_plain_true_is_remembered():
    cache = new_cache()

    async def odd(raw, hashed):
        return 1  # truthy, but not the answer bcrypt gives

    assert asyncio.run(verify_password_remembered("x", HASH_A, odd, cache=cache)) == 1
    assert len(cache) == 0


def test_a_failed_check_leaves_nothing_behind():
    cache = new_cache()

    async def broken(raw, hashed):
        raise RuntimeError("the hashing pool is gone")

    try:
        asyncio.run(verify_password_remembered("right", HASH_A, broken, cache=cache))
    except RuntimeError:
        pass
    else:
        raise AssertionError("the error must reach the caller")
    assert len(cache) == 0


def test_logins_at_the_same_moment():
    cache, verifier = new_cache(), Verifier(("right", HASH_A))

    async def burst(passwords):
        return await asyncio.gather(*(verify_password_remembered(p, HASH_A, verifier, cache=cache) for p in passwords))

    # nothing remembered at the start: each gets the real check, until the first one is remembered
    assert asyncio.run(burst(["right"] * 6)) == [True] * 6
    first = len(verifier.calls)
    assert 1 <= first <= 6 and len(cache) == 1
    # now remembered: right ones are answered from memory, wrong ones are all hashed
    mixed = ["right", "wrong", "right", "nope", "right", "wrong"]
    assert asyncio.run(burst(mixed)) == [True, False, True, False, True, False]
    assert len(verifier.calls) == first + 3
    assert sorted(c[0] for c in verifier.calls[first:]) == ["nope", "wrong", "wrong"]


def test_the_cache_stays_small():
    clock = Clock()
    cache = new_cache(max_entries=3, clock=clock)
    for i in range(10):
        cache.remember(f"hash{i}", cache.proof("p", f"hash{i}"))
    assert len(cache) == 3
    assert cache.matches("hash9", cache.proof("p", "hash9"))
    assert not cache.matches("hash0", cache.proof("p", "hash0"))


def test_window_from_the_setting():
    assert seconds_from_setting(None) == DEFAULT_SECONDS == 300
    assert seconds_from_setting("") == DEFAULT_SECONDS and seconds_from_setting("  ") == DEFAULT_SECONDS
    assert seconds_from_setting("0") == 0 and seconds_from_setting(" 60 ") == 60
    assert seconds_from_setting("-5") == 0
    assert seconds_from_setting("999999") == MAX_SECONDS == 3600
    assert VerifiedLoginCache(10**9).seconds == MAX_SECONDS
    # an attempt to switch it off that cannot be read must not leave it on
    for unreadable in ("off", "false", "no", "0.0", "0s", "five"):
        assert seconds_from_setting(unreadable) == 0, unreadable


def test_the_setting_is_read_from_the_env_file_too(tmp_path, monkeypatch):
    """The rest of the panel reads `.env` without exporting it; this setting must not be the exception."""
    monkeypatch.delenv("PENGU_LOGIN_CACHE_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    assert login_cache._Settings().seconds is None
    (tmp_path / ".env").write_text("OTHER=1\nPENGU_LOGIN_CACHE_SECONDS=0\n", encoding="utf-8")
    assert seconds_from_setting(login_cache._Settings().seconds) == 0
    monkeypatch.setenv("PENGU_LOGIN_CACHE_SECONDS", "120")
    assert seconds_from_setting(login_cache._Settings().seconds) == 120


def test_answers_always_equal_bcrypt():
    """With the real bcrypt behind it, every answer is the one bcrypt alone would give."""
    stored = {
        name: bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
        for name, password in (("a", "Alpha#12"), ("b", "Beta#34"))
    }
    hashed_through_the_cache = 0

    def bcrypt_alone(raw: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
        except ValueError:
            return False

    async def real(raw: str, hashed: str) -> bool:
        nonlocal hashed_through_the_cache
        hashed_through_the_cache += 1
        return bcrypt_alone(raw, hashed)

    cache = new_cache()
    attempts = ["Alpha#12", "Beta#34", "alpha#12", "Alpha#12 ", "", "Alpha#1", "x" * 80, "Beta#34"] * 3
    wrong = 0
    for hashed in stored.values():
        for raw in attempts:
            expected = bcrypt_alone(raw, hashed)
            wrong += not expected
            assert asyncio.run(verify_password_remembered(raw, hashed, real, cache=cache)) is expected, (raw, hashed)
    # 48 attempts: 39 wrong ones each hashed in full, and one hash per admin for the 9 right ones
    assert wrong == 39 and hashed_through_the_cache == 39 + 2
