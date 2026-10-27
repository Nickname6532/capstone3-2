"""로그인/계정 관리 보안 동작을 검증한다: 비밀번호 해시, 잠금, 권한 분리,
세션 보호, 응답에 해시가 절대 섞이지 않는지."""

import importlib

import pytest
from fastapi.testclient import TestClient

from auth import hash_password, verify_password


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    import app as app_module
    from claim_logger import ClaimLogger
    from claims_store import ClaimsStore

    importlib.reload(app_module)
    test_logger = ClaimLogger(base_dir=tmp_path / "logs")
    test_store = ClaimsStore(base_dir=tmp_path / "data", logger=test_logger)
    monkeypatch.setattr(app_module, "logger", test_logger)
    monkeypatch.setattr(app_module, "store", test_store)

    test_store.create_account("admin1", hash_password("AdminPass123!"), "admin", "관리자")
    test_store.create_account("staff1", hash_password("StaffPass123!"), "staff", "담당자")

    return TestClient(app_module.app), test_store


def test_password_hash_roundtrip():
    h = hash_password("Correct123!")
    assert verify_password("Correct123!", h)
    assert not verify_password("wrong-password", h)


def test_password_never_stored_in_plaintext():
    h = hash_password("PlainPassword123")
    assert "PlainPassword123" not in h
    assert h.startswith("$2b$")  # bcrypt


def test_unknown_user_and_wrong_password_get_identical_generic_message(app_client):
    client, _ = app_client
    r1 = client.post("/api/auth/login", json={"username": "ghost", "password": "whatever123"})
    r2 = client.post("/api/auth/login", json={"username": "admin1", "password": "wrongpassword"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]


def test_protected_route_requires_login(app_client):
    client, _ = app_client
    res = client.get("/api/claims")
    assert res.status_code == 401


def test_login_success_grants_access(app_client):
    client, _ = app_client
    res = client.post("/api/auth/login", json={"username": "staff1", "password": "StaffPass123!"})
    assert res.status_code == 200
    assert res.json()["role"] == "staff"

    res = client.get("/api/claims")
    assert res.status_code == 200


def test_staff_cannot_access_admin_endpoints(app_client):
    client, _ = app_client
    client.post("/api/auth/login", json={"username": "staff1", "password": "StaffPass123!"})

    res = client.get("/api/admin/accounts")
    assert res.status_code == 403

    res = client.post("/api/admin/accounts", json={"username": "new1", "password": "NewPass1234", "role": "staff"})
    assert res.status_code == 403


def test_admin_can_create_account_and_response_excludes_password_hash(app_client):
    client, _ = app_client
    client.post("/api/auth/login", json={"username": "admin1", "password": "AdminPass123!"})

    res = client.post(
        "/api/admin/accounts",
        json={"username": "new_staff", "password": "NewStaffPass123", "role": "staff", "display_name": "신규"},
    )
    assert res.status_code == 200
    assert "password_hash" not in res.json()

    res = client.get("/api/admin/accounts")
    assert res.status_code == 200
    for account in res.json():
        assert "password_hash" not in account


def test_admin_create_account_rejects_weak_password(app_client):
    client, _ = app_client
    client.post("/api/auth/login", json={"username": "admin1", "password": "AdminPass123!"})

    res = client.post("/api/admin/accounts", json={"username": "weak1", "password": "12345678901", "role": "staff"})
    assert res.status_code == 400


def test_duplicate_username_rejected(app_client):
    client, _ = app_client
    client.post("/api/auth/login", json={"username": "admin1", "password": "AdminPass123!"})

    res = client.post("/api/admin/accounts", json={"username": "staff1", "password": "AnotherPass123", "role": "staff"})
    assert res.status_code == 409


def test_login_locks_out_after_repeated_failures(app_client):
    client, store = app_client
    for _ in range(5):
        res = client.post("/api/auth/login", json={"username": "staff1", "password": "wrongpassword"})
        assert res.status_code == 401

    # 6번째는 올바른 비밀번호를 넣어도 잠금 때문에 거부되어야 한다
    res = client.post("/api/auth/login", json={"username": "staff1", "password": "StaffPass123!"})
    assert res.status_code == 429


def test_logout_revokes_session(app_client):
    client, _ = app_client
    client.post("/api/auth/login", json={"username": "staff1", "password": "StaffPass123!"})
    assert client.get("/api/claims").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/claims").status_code == 401


def test_root_redirects_to_login_when_unauthenticated(app_client):
    client, _ = app_client
    res = client.get("/", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"] == "/login"


def test_submit_and_public_claim_do_not_require_login(app_client):
    client, _ = app_client
    res = client.post(
        "/api/public/claims",
        json={"channel": "웹", "customer": "고객", "contact": "010-0000-0000", "product": "P", "description": "D"},
    )
    assert res.status_code == 200
