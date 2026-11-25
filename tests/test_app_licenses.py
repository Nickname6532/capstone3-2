"""라이선스 관련 API 엔드포인트(인증 포함) 검증."""

import importlib
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app as app_module
    from auth import hash_password
    from claim_logger import ClaimLogger
    from claims_store import ClaimsStore

    importlib.reload(app_module)
    test_logger = ClaimLogger(base_dir=tmp_path / "logs")
    test_store = ClaimsStore(base_dir=tmp_path / "data", logger=test_logger)
    test_store.create_account("tester", hash_password("Tester1234!!"), "staff", "테스터")
    monkeypatch.setattr(app_module, "logger", test_logger)
    monkeypatch.setattr(app_module, "store", test_store)
    monkeypatch.setattr(app_module, "CRON_SECRET", "test-cron-secret")

    test_client = TestClient(app_module.app)
    test_client.post("/api/auth/login", json={"username": "tester", "password": "Tester1234!!"})
    return test_client


def _iso(days):
    return (date.today() + timedelta(days=days)).isoformat()


def test_license_endpoints_require_login():
    import app as app_module

    anon = TestClient(app_module.app)
    assert anon.get("/api/customers").status_code == 401
    assert anon.get("/api/licenses").status_code == 401
    assert anon.get("/api/licenses/expiring").status_code == 401


def test_create_customer_product_license_via_api(client):
    res = client.post("/api/customers", json={"category": "테스트", "name": "가나전자", "contact": ""})
    assert res.status_code == 200
    customer_id = res.json()["id"]

    res = client.post("/api/products", json={"customer_id": customer_id, "name": "냉장고 K100"})
    assert res.status_code == 200
    product_id = res.json()["id"]

    res = client.post(
        "/api/licenses",
        json={
            "product_id": product_id,
            "plan_name": "Standard",
            "start_date": _iso(-10),
            "end_date": _iso(10),
            "notify_emails": "ops@company.com",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "만료임박"

    res = client.get("/api/licenses/expiring")
    assert res.status_code == 200
    assert len(res.json()) == 1


def test_create_license_rejects_end_before_start(client):
    res = client.post("/api/customers", json={"category": "테스트", "name": "고객"})
    customer_id = res.json()["id"]
    res = client.post("/api/products", json={"customer_id": customer_id, "name": "제품"})
    product_id = res.json()["id"]

    res = client.post(
        "/api/licenses",
        json={"product_id": product_id, "start_date": _iso(10), "end_date": _iso(0)},
    )
    assert res.status_code == 400


def test_check_expiry_requires_login_or_cron_secret(client):
    import app as app_module

    anon = TestClient(app_module.app)
    assert anon.get("/api/licenses/check-expiry").status_code == 401

    res = anon.get("/api/licenses/check-expiry", headers={"Authorization": "Bearer wrong-secret"})
    assert res.status_code == 401

    res = anon.get("/api/licenses/check-expiry", headers={"Authorization": "Bearer test-cron-secret"})
    assert res.status_code == 200

    # 로그인한 사용자는 비밀 헤더 없이도 수동 호출 가능
    res = client.get("/api/licenses/check-expiry")
    assert res.status_code == 200
