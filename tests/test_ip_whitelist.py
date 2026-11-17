"""IP 화이트리스트 미들웨어 검증. _ALLOWED_NETWORKS는 import 시점에 한 번 계산되므로
환경변수를 먼저 설정한 뒤 app 모듈을 reload해야 한다."""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def restricted_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWED_IPS", "203.0.113.0/24")  # RFC 5737 TEST-NET-3

    import app as app_module
    from auth import hash_password
    from claim_logger import ClaimLogger
    from claims_store import ClaimsStore

    importlib.reload(app_module)
    test_logger = ClaimLogger(base_dir=tmp_path / "logs")
    test_store = ClaimsStore(base_dir=tmp_path / "data", logger=test_logger)
    test_store.create_account("tester", hash_password("Tester1234!!"), "admin", "테스터")
    monkeypatch.setattr(app_module, "logger", test_logger)
    monkeypatch.setattr(app_module, "store", test_store)

    yield TestClient(app_module.app)

    monkeypatch.delenv("ALLOWED_IPS", raising=False)
    importlib.reload(app_module)


def test_internal_path_blocked_from_disallowed_ip(restricted_client):
    assert restricted_client.get("/").status_code == 403


def test_public_submit_path_allowed_regardless_of_ip(restricted_client):
    assert restricted_client.get("/submit").status_code == 200


def test_public_api_allowed_regardless_of_ip(restricted_client):
    res = restricted_client.post(
        "/api/public/claims",
        json={"customer": "A", "contact": "010-1234-5678", "product": "P", "description": "D"},
    )
    assert res.status_code == 200


def test_cron_endpoint_allowed_with_bearer_token_regardless_of_ip(restricted_client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "CRON_SECRET", "test-secret")
    res = restricted_client.get("/api/licenses/check-expiry", headers={"Authorization": "Bearer test-secret"})
    assert res.status_code == 200


def test_login_is_restricted_without_allowed_ip(restricted_client):
    assert restricted_client.get("/login").status_code == 403


def test_no_allowed_ips_means_unrestricted(monkeypatch):
    monkeypatch.delenv("ALLOWED_IPS", raising=False)
    import app as app_module

    importlib.reload(app_module)
    assert TestClient(app_module.app).get("/login").status_code == 200
