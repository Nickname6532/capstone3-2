"""고객사/제품/라이선스 CRUD, 처리이력, 고객 티켓 조회, 통계 분석, 매뉴얼/추천, 계정 관리,
첨부파일, 라이선스 가져오기 등 확장 API 검증."""

import importlib
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app as app_module
    from auth import hash_password
    from claim_logger import ClaimLogger
    from claims_store import ClaimsStore
    from storage import LocalAttachmentStorage

    importlib.reload(app_module)
    test_logger = ClaimLogger(base_dir=tmp_path / "logs")
    test_store = ClaimsStore(base_dir=tmp_path / "data", logger=test_logger)
    test_store.create_account("tester", hash_password("Tester1234!!"), "admin", "테스터")
    monkeypatch.setattr(app_module, "logger", test_logger)
    monkeypatch.setattr(app_module, "store", test_store)
    monkeypatch.setattr(app_module, "attachment_storage", LocalAttachmentStorage(tmp_path / "attachments"))

    test_client = TestClient(app_module.app)
    assert test_client.post("/api/auth/login", json={"username": "tester", "password": "Tester1234!!"}).status_code == 200
    return test_client


def _iso(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _public_claim(client, contact="010-1234-5678"):
    res = client.post("/api/public/claims", json={"customer": "홍길동", "contact": contact, "product": "Pro", "description": "오류"})
    return res.json()["claim_id"]


# ---------------------------------------------------------------- 처리상세 / 첨부파일

def test_create_claim_accepts_processing_detail_fields(client):
    res = client.post("/api/claims", json={"customer": "넷스퀘어", "product": "Pro 플랜", "description": "로그인 불가",
                                           "processing_type": "엔지니어방문", "difficulty": "high"})
    assert res.status_code == 200
    assert res.json()["processing_type"] == "엔지니어방문" and res.json()["difficulty"] == "high"


def test_patch_claim_rejects_unknown_processing_type(client):
    created = client.post("/api/claims", json={"customer": "A", "product": "P", "description": "D"}).json()
    assert client.patch(f"/api/claims/{created['claim_id']}", json={"processing_type": "텔레포트"}).status_code == 400


def test_attachment_upload_list_download_delete(client):
    claim_id = client.post("/api/claims", json={"customer": "A", "product": "P", "description": "D"}).json()["claim_id"]
    att = client.post(f"/api/claims/{claim_id}/attachments", files={"file": ("screenshot.png", PNG, "image/png")}).json()
    assert att["filename"] == "screenshot.png"
    assert len(client.get(f"/api/claims/{claim_id}/attachments").json()) == 1
    downloaded = client.get(f"/api/attachments/{att['id']}/file")
    assert downloaded.status_code == 200 and downloaded.content == PNG
    assert client.delete(f"/api/attachments/{att['id']}").status_code == 200
    assert client.get(f"/api/claims/{claim_id}/attachments").json() == []


def test_attachment_upload_rejects_disallowed_type_oversize_and_unknown_claim(client, monkeypatch):
    import app as app_module

    claim_id = client.post("/api/claims", json={"customer": "A", "product": "P", "description": "D"}).json()["claim_id"]
    assert client.post(f"/api/claims/{claim_id}/attachments", files={"file": ("v.exe", b"MZ", "application/x-msdownload")}).status_code == 400
    monkeypatch.setattr(app_module, "MAX_ATTACHMENT_BYTES", 10)
    assert client.post(f"/api/claims/{claim_id}/attachments", files={"file": ("b.png", b"0" * 100, "image/png")}).status_code == 400
    assert client.post("/api/claims/CLM-NOPE-000/attachments", files={"file": ("a.png", b"abc", "image/png")}).status_code == 404


def test_attachment_endpoints_require_login():
    import app as app_module

    anon = TestClient(app_module.app)
    assert anon.get("/api/claims/CLM-NOPE-000/attachments").status_code == 401
    assert anon.get("/api/attachments/1/file").status_code == 401


# ---------------------------------------------------------------- 고객사/제품/라이선스 CRUD + overview

def test_customer_product_license_crud_endpoints(client):
    cust = client.post("/api/customers", json={"name": "가나전자", "email": "ops@gana.com"}).json()
    assert client.patch(f"/api/customers/{cust['id']}", json={"memo": "VIP"}).json()["memo"] == "VIP"
    prod = client.post("/api/products", json={"customer_id": cust["id"], "name": "냉장고"}).json()
    client.patch(f"/api/products/{prod['id']}", json={"name": "냉장고 K200"})
    lic = client.post("/api/licenses", json={"product_id": prod["id"], "start_date": _iso(-10), "end_date": _iso(10), "warning_days": 7}).json()
    assert lic["warning_days"] == 7
    client.patch(f"/api/licenses/{lic['id']}", json={"notify_target_option": "staff"})
    assert client.delete(f"/api/licenses/{lic['id']}").status_code == 200
    assert client.delete(f"/api/products/{prod['id']}").status_code == 200
    assert client.delete(f"/api/customers/{cust['id']}").status_code == 200


def test_delete_customer_with_products_returns_409(client):
    cust = client.post("/api/customers", json={"name": "가나전자"}).json()
    client.post("/api/products", json={"customer_id": cust["id"], "name": "냉장고"})
    assert client.delete(f"/api/customers/{cust['id']}").status_code == 409


def test_customer_overview_endpoint(client):
    cust = client.post("/api/customers", json={"name": "가나전자"}).json()
    prod = client.post("/api/products", json={"customer_id": cust["id"], "name": "냉장고"}).json()
    client.post("/api/licenses", json={"product_id": prod["id"], "start_date": _iso(-10), "end_date": _iso(10)})
    client.post("/api/claims", json={"customer": "가나전자", "product": "냉장고", "description": "D", "customer_id": cust["id"]})
    overview = client.get(f"/api/customers/{cust['id']}/overview").json()
    assert overview["claim_count"] == 1 and len(overview["licenses"]) == 1


# ---------------------------------------------------------------- 처리이력 / 확장 필드

def test_claim_history_endpoint(client):
    claim = client.post("/api/claims", json={"customer": "A", "product": "P", "description": "D"}).json()
    client.patch(f"/api/claims/{claim['claim_id']}", json={"status": "처리중", "note": "접수 확인"})
    history = client.get(f"/api/claims/{claim['claim_id']}/history").json()
    assert len(history) == 2 and history[-1]["note"] == "접수 확인" and history[-1]["changed_by"] == "테스터"


def test_claim_extended_processing_fields_round_trip(client):
    claim = client.post("/api/claims", json={
        "customer": "A", "product": "P", "description": "D", "sub_status": "엔지니어방문예정", "processing_minutes": 45,
        "partner_name": "협력업체A", "partner_contact": "02-111-2222", "scheduled_visit_at": "2026-11-20 10:00"}).json()
    assert claim["sub_status"] == "엔지니어방문예정" and claim["partner_name"] == "협력업체A"
    updated = client.patch(f"/api/claims/{claim['claim_id']}", json={"completed_visit_at": "2026-11-20 11:00"}).json()
    assert updated["completed_visit_at"] == "2026-11-20 11:00"


# ---------------------------------------------------------------- 고객 티켓 조회

def test_public_track_requires_matching_contact(client):
    claim_id = _public_claim(client)
    ok = client.post("/api/public/claims/track", json={"claim_id": claim_id, "contact": "010-1234-5678"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "접수" and "cost_krw" not in body and "assignee" not in body
    assert client.post("/api/public/claims/track", json={"claim_id": claim_id, "contact": "010-0000-0000"}).status_code == 404


def test_public_track_allows_last_4_digits(client):
    claim_id = _public_claim(client)
    assert client.post("/api/public/claims/track", json={"claim_id": claim_id, "contact": "5678"}).status_code == 200


def test_public_attachment_upload_verification_and_limit(client):
    claim_id = _public_claim(client)
    url = f"/api/public/claims/{claim_id}/attachments"
    assert client.post(url, data={"contact": "010-0000-0000"}, files={"file": ("a.png", PNG, "image/png")}).status_code == 404
    for _ in range(3):
        assert client.post(url, data={"contact": "010-1234-5678"}, files={"file": ("a.png", PNG, "image/png")}).status_code == 200
    assert client.post(url, data={"contact": "010-1234-5678"}, files={"file": ("a.png", PNG, "image/png")}).status_code == 400


def test_public_note_allowed_before_done_blocked_after(client):
    claim_id = _public_claim(client)
    assert client.post(f"/api/public/claims/{claim_id}/notes", json={"contact": "010-1234-5678", "body": "사진 추가"}).status_code == 200
    tracked = client.post("/api/public/claims/track", json={"claim_id": claim_id, "contact": "010-1234-5678"}).json()
    assert len(tracked["notes"]) == 1
    client.patch(f"/api/claims/{claim_id}", json={"status": "처리중"})
    client.patch(f"/api/claims/{claim_id}", json={"status": "완료"})
    assert client.post(f"/api/public/claims/{claim_id}/notes", json={"contact": "010-1234-5678", "body": "또"}).status_code == 400


# ---------------------------------------------------------------- 통계 / 가져오기

def test_stats_analytics_shape(client):
    claim = client.post("/api/claims", json={"customer": "A", "product": "P", "description": "D", "assignee": "김담당",
                                             "processing_minutes": 30, "difficulty": "medium"}).json()
    client.patch(f"/api/claims/{claim['claim_id']}", json={"status": "처리중"})
    client.patch(f"/api/claims/{claim['claim_id']}", json={"status": "완료"})
    body = client.get("/api/stats/analytics").json()
    for key in ("by_assignee", "by_processing_type", "by_difficulty", "by_category", "by_customer_top", "monthly_trend"):
        assert key in body
    assert next(r for r in body["by_assignee"] if r["assignee"] == "김담당")["done"] == 1


def test_stats_export_csv(client):
    client.post("/api/claims", json={"customer": "A", "product": "P", "description": "D"})
    res = client.get("/api/stats/export.csv")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv") and "접수번호" in res.text


def test_import_dry_run_does_not_write_to_db(client):
    csv_content = "고객사,제품,내용\n가나전자,냉장고,문제1\n".encode("utf-8-sig")
    res = client.post("/api/claims/import", params={"dry_run": "true"}, files={"file": ("data.csv", csv_content, "text/csv")})
    body = res.json()
    assert body["dry_run"] is True and body["imported_count"] == 0 and len(body["preview_rows"]) == 1
    assert client.get("/api/claims").json() == []


def test_import_auto_create_master_links_customer_and_product(client):
    csv_content = "고객사,제품,내용\n넷스퀘어,Pro 플랜,로그인 오류\n".encode("utf-8-sig")
    res = client.post("/api/claims/import", params={"auto_create_master": "true"}, files={"file": ("data.csv", csv_content, "text/csv")})
    assert res.json()["imported_count"] == 1
    assert any(c["name"] == "넷스퀘어" for c in client.get("/api/customers").json())
    claim = client.get("/api/claims").json()[0]
    assert claim["customer_id"] is not None and claim["product_id"] is not None


VMWARE_CSV = (
    "No,라이선스 키,사이트명,구분,구독기간(년),시작일,만료일,남은 일수,상태\n"
    "1,JHNJOMXXO-2K1M9YRWC,삼선전자,코스피,5,2026-05-07,2031-05-07,1639,활성\n"
    "2,JO06I63PL-63HUV284Z,삼선전자,코스피,1,2026-05-18,2027-05-18,189,활성\n"
    "3,OFL4V7WY4-5HQFKMWXG,계몽대학교,대학,3,2026-01-11,2029-01-11,793,활성\n"
).encode("utf-8-sig")


def test_license_import_dry_run_does_not_write_to_db(client):
    res = client.post("/api/licenses/import", params={"dry_run": "true"}, files={"file": ("v.csv", VMWARE_CSV, "text/csv")})
    body = res.json()
    assert body["dry_run"] is True and body["imported_count"] == 0 and len(body["preview_rows"]) == 3
    assert client.get("/api/customers").json() == []


def test_license_import_creates_customers_products_and_licenses(client):
    res = client.post("/api/licenses/import", params={"default_product_name": "VMware"}, files={"file": ("v.csv", VMWARE_CSV, "text/csv")})
    assert res.json()["imported_count"] == 3
    customers = client.get("/api/customers").json()
    assert {c["name"] for c in customers} == {"삼선전자", "계몽대학교"}
    assert next(c for c in customers if c["name"] == "삼선전자")["memo"] == "코스피"
    sam = [l for l in client.get("/api/licenses").json() if l["customer_name"] == "삼선전자"]
    assert len(sam) == 2 and all(l["product_name"] == "VMware" for l in sam)
    assert {l["license_key"] for l in sam} == {"JHNJOMXXO-2K1M9YRWC", "JO06I63PL-63HUV284Z"}
    assert {l["plan_name"] for l in sam} == {"5년", "1년"}


def test_license_import_skips_duplicate_license_keys_on_rerun(client):
    args = dict(params={"default_product_name": "VMware"}, files={"file": ("v.csv", VMWARE_CSV, "text/csv")})
    client.post("/api/licenses/import", **args)
    body2 = client.post("/api/licenses/import", **args).json()
    assert body2["imported_count"] == 0 and body2["duplicate_count"] == 3
    assert len(client.get("/api/licenses").json()) == 3


# ---------------------------------------------------------------- 매뉴얼 / 추천

def test_manuals_crud_endpoints(client):
    manual = client.post("/api/manuals", json={"title": "SSO 오류 대응", "category": "로그인", "keywords": "SSO,500", "content": "토큰 캐시 삭제"}).json()
    client.patch(f"/api/manuals/{manual['id']}", json={"content": "수정됨"})
    assert len(client.get("/api/manuals", params={"q": "SSO"}).json()) == 1
    assert client.delete(f"/api/manuals/{manual['id']}").status_code == 200


def test_claim_recommendations_endpoint(client):
    old = client.post("/api/claims", json={"customer": "X", "product": "P", "category": "C", "description": "D1"}).json()
    client.patch(f"/api/claims/{old['claim_id']}", json={"status": "처리중"})
    client.patch(f"/api/claims/{old['claim_id']}", json={"status": "완료", "resolution": "해결함"})
    current = client.post("/api/claims", json={"customer": "Y", "product": "P", "category": "C", "description": "D2"}).json()
    res = client.get(f"/api/claims/{current['claim_id']}/recommendations").json()
    assert len(res["similar_claims"]) == 1 and res["similar_claims"][0]["claim_id"] == old["claim_id"]


# ---------------------------------------------------------------- 계정 관리 (admin 전용)

def test_account_deactivate_blocks_login(client):
    client.post("/api/admin/accounts", json={"username": "staffer", "password": "StaffPass123", "role": "staff"})
    client.patch("/api/admin/accounts/staffer", json={"is_active": False})
    anon = TestClient(client.app)
    assert anon.post("/api/auth/login", json={"username": "staffer", "password": "StaffPass123"}).status_code == 401


def test_account_unlock_and_password_reset(client):
    client.post("/api/admin/accounts", json={"username": "staffer2", "password": "StaffPass123", "role": "staff"})
    anon = TestClient(client.app)
    for _ in range(5):
        anon.post("/api/auth/login", json={"username": "staffer2", "password": "wrong"})
    assert anon.post("/api/auth/login", json={"username": "staffer2", "password": "StaffPass123"}).status_code == 429
    client.post("/api/admin/accounts/staffer2/unlock")
    client.post("/api/admin/accounts/staffer2/reset-password", json={"new_password": "NewPass456"})
    assert anon.post("/api/auth/login", json={"username": "staffer2", "password": "NewPass456"}).status_code == 200


def test_staff_role_cannot_manage_accounts(client):
    client.post("/api/admin/accounts", json={"username": "staffer3", "password": "StaffPass123", "role": "staff"})
    staff = TestClient(client.app)
    login = staff.post("/api/auth/login", json={"username": "staffer3", "password": "StaffPass123"})
    assert login.status_code == 200 and login.json()["role"] == "staff"
    assert staff.get("/api/admin/accounts").status_code == 403
    assert staff.post("/api/admin/accounts", json={"username": "newstaff", "password": "SomePass123", "role": "staff"}).status_code == 403
    assert staff.patch("/api/admin/accounts/staffer3", json={"is_active": False}).status_code == 403
    assert staff.post("/api/admin/accounts/staffer3/unlock").status_code == 403
    assert staff.post("/api/admin/accounts/staffer3/reset-password", json={"new_password": "HackedPass123"}).status_code == 403
    assert staff.get("/admin").status_code == 403


def test_admin_can_reset_another_admins_password(client):
    client.post("/api/admin/accounts", json={"username": "otheradmin", "password": "OtherAdminPass123", "role": "admin"})
    assert client.post("/api/admin/accounts/otheradmin/reset-password", json={"new_password": "ResetByPeerAdmin1"}).status_code == 200
    ok = TestClient(client.app).post("/api/auth/login", json={"username": "otheradmin", "password": "ResetByPeerAdmin1"})
    assert ok.status_code == 200 and ok.json()["role"] == "admin"
