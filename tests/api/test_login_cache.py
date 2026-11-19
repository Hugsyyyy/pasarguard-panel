"""API tests of remembered logins (Pengu fork): /api/admin/token answers exactly as before."""

import pytest
from fastapi import status

from app.core import login_cache
from app.models import admin as admin_models
from tests.api import client
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password


@pytest.fixture
def hashing(monkeypatch: pytest.MonkeyPatch):
    """A fresh cache, and a count of the real bcrypt checks."""
    monkeypatch.setattr(login_cache, "_cache", login_cache.VerifiedLoginCache(300))
    calls: list[str] = []
    real = admin_models._verify_password_sync

    def counted(raw: str, hashed: str) -> bool:
        calls.append(raw)
        return real(raw, hashed)

    monkeypatch.setattr(admin_models, "_verify_password_sync", counted)
    return calls


def login(username: str, password: str):
    return client.post("/api/admin/token", data={"username": username, "password": password, "grant_type": "password"})


def test_repeated_logins_hash_once_and_wrong_passwords_always(access_token, hashing):
    admin = create_admin(access_token)
    try:
        tokens = []
        for _ in range(5):
            response = login(admin["username"], admin["password"])
            assert response.status_code == status.HTTP_200_OK
            tokens.append(response.json()["access_token"])
        assert hashing == [admin["password"]], "five logins with the right password: one real check"
        # every token is a normal, working token
        for token in tokens:
            assert client.get("/api/admin", headers=auth_headers(token)).status_code == status.HTTP_200_OK

        for guess in ("wrong-password", admin["password"] + "x", admin["password"][:-1]):
            assert login(admin["username"], guess).status_code == status.HTTP_401_UNAUTHORIZED, guess
        assert len(hashing) == 4, "each wrong guess costs a full check, even next to a remembered login"
        # an empty password never reaches the check at all, as before
        assert login(admin["username"], "").status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert login(admin["username"], admin["password"]).status_code == status.HTTP_200_OK
        assert len(hashing) == 4
    finally:
        delete_admin(access_token, admin["username"])


def test_a_changed_password_stops_the_old_one_at_once(access_token, hashing):
    admin = create_admin(access_token)
    try:
        assert login(admin["username"], admin["password"]).status_code == status.HTTP_200_OK
        assert login(admin["username"], admin["password"]).status_code == status.HTTP_200_OK
        new_password = strong_password("ChangedLogin")
        changed = client.put(
            f"/api/admin/{admin['username']}", json={"password": new_password}, headers=auth_headers(access_token)
        )
        assert changed.status_code == status.HTTP_200_OK
        assert login(admin["username"], admin["password"]).status_code == status.HTTP_401_UNAUTHORIZED
        assert login(admin["username"], new_password).status_code == status.HTTP_200_OK
    finally:
        delete_admin(access_token, admin["username"])


def test_a_disabled_admin_is_refused_even_when_remembered(access_token, hashing):
    admin = create_admin(access_token)
    try:
        assert login(admin["username"], admin["password"]).status_code == status.HTTP_200_OK
        disabled = client.put(
            f"/api/admin/{admin['username']}", json={"status": "disabled"}, headers=auth_headers(access_token)
        )
        assert disabled.status_code == status.HTTP_200_OK and disabled.json()["status"] == "disabled"
        assert login(admin["username"], admin["password"]).status_code == status.HTTP_403_FORBIDDEN
    finally:
        delete_admin(access_token, admin["username"])


def test_a_deleted_admin_is_refused_even_when_remembered(access_token, hashing):
    admin = create_admin(access_token)
    assert login(admin["username"], admin["password"]).status_code == status.HTTP_200_OK
    delete_admin(access_token, admin["username"])
    assert login(admin["username"], admin["password"]).status_code == status.HTTP_401_UNAUTHORIZED


def test_one_admins_password_does_not_open_another(access_token, hashing):
    first, second = create_admin(access_token), create_admin(access_token)
    try:
        assert login(first["username"], first["password"]).status_code == status.HTTP_200_OK
        assert login(second["username"], first["password"]).status_code == status.HTTP_401_UNAUTHORIZED
        assert login(second["username"], second["password"]).status_code == status.HTTP_200_OK
        assert login(first["username"], second["password"]).status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        delete_admin(access_token, first["username"])
        delete_admin(access_token, second["username"])


def test_switched_off_every_login_is_hashed(access_token, hashing, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(login_cache, "_cache", login_cache.VerifiedLoginCache(0))
    admin = create_admin(access_token)
    try:
        for _ in range(3):
            assert login(admin["username"], admin["password"]).status_code == status.HTTP_200_OK
        assert len(hashing) == 3
    finally:
        delete_admin(access_token, admin["username"])
