import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """app.py 모듈을 불러온 뒤 전역 store/logger를 tmp_path로 갈아끼운다.

    실제 개발용 데이터(src/data, src/logs)를 건드리지 않기 위함이다.
    """
    import app as app_module
    from claim_logger import ClaimLogger
    from claims_store import ClaimsStore

    importlib.reload(app_module)  # 이전 테스트의 monkeypatch 잔재 제거
    test_logger = ClaimLogger(base_dir=tmp_path / "logs")
    test_store = ClaimsStore(base_dir=tmp_path / "data", logger=test_logger)
    monkeypatch.setattr(app_module, "logger", test_logger)
    monkeypatch.setattr(app_module, "store", test_store)

    return TestClient(app_module.app)


def test_create_and_list_claim(client):
    res = client.post(
        "/api/claims",
        json={"customer": "가나전자", "product": "냉장고", "description": "문이 안닫힘"},
    )
    assert res.status_code == 200
    claim_id = res.json()["claim_id"]

    res = client.get("/api/claims")
    assert res.status_code == 200
    assert any(c["claim_id"] == claim_id for c in res.json())


def test_public_claim_rejects_invalid_channel(client):
    res = client.post(
        "/api/public/claims",
        json={
            "channel": "내부입력",
            "customer": "홍길동",
            "contact": "010-0000-0000",
            "product": "세탁기",
            "description": "소음",
        },
    )
    assert res.status_code == 400


def test_patch_invalid_transition_returns_400(client):
    res = client.post(
        "/api/claims", json={"customer": "A", "product": "P", "description": "D"}
    )
    claim_id = res.json()["claim_id"]
    client.patch(f"/api/claims/{claim_id}", json={"status": "완료"})

    res = client.patch(f"/api/claims/{claim_id}", json={"status": "처리중"})
    assert res.status_code == 400


def test_patch_unknown_claim_returns_404(client):
    res = client.patch("/api/claims/CLM-NOPE-000", json={"status": "완료"})
    assert res.status_code == 404


def test_sms_webhook_creates_claim(client):
    res = client.post(
        "/api/channels/sms", json={"from_number": "010-1234-5678", "text": "제품 고장"}
    )
    assert res.status_code == 200
    assert res.json()["channel"] == "문자"


def test_bulk_import_endpoint_reports_counts(client):
    csv_content = (
        "고객사,제품,내용,상태\n"
        "가나전자,냉장고,문제1,완료\n"
        "다라산업,보일러,문제2,접수\n"
        ",누락,고객사 없음,접수\n"
    ).encode("utf-8-sig")

    res = client.post(
        "/api/claims/import",
        files={"file": ("data.csv", csv_content, "text/csv")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["imported_count"] == 2
    assert body["failed_count"] == 1

    # 같은 파일을 다시 올리면 이번엔 전부 중복으로 잡혀야 한다
    res2 = client.post(
        "/api/claims/import",
        files={"file": ("data.csv", csv_content, "text/csv")},
    )
    body2 = res2.json()
    assert body2["imported_count"] == 0
    assert body2["duplicate_count"] == 2


def test_bulk_import_rejects_bad_file_with_400(client):
    res = client.post(
        "/api/claims/import",
        files={"file": ("data.xlsx", b"not a real xlsx", "application/octet-stream")},
    )
    assert res.status_code == 400


def test_stats_summary_shape(client):
    client.post(
        "/api/claims", json={"customer": "A", "product": "P", "description": "D"}
    )
    res = client.get("/api/stats/summary")
    assert res.status_code == 200
    body = res.json()
    for key in ("received", "in_progress", "done", "channel_breakdown"):
        assert key in body
