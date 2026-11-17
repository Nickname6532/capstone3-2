"""PgClaimsStore 테스트. 로컬 Postgres가 없으면 자동으로 스킵한다.

CI/로컬에서 검증하려면:
    createdb claim_test
    TEST_DATABASE_URL=postgresql://localhost:5432/claim_test pytest tests/test_claims_store_pg.py
"""

import os

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost:5432/claim_test")

psycopg = pytest.importorskip("psycopg")

try:
    _probe = psycopg.connect(TEST_DATABASE_URL, connect_timeout=2)
    _probe.close()
    PG_AVAILABLE = True
except Exception:
    PG_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="로컬 Postgres(claim_test)에 연결할 수 없음")

from claims_store import (
    DIFFICULTY_HIGH,
    PROCESSING_ENGINEER_VISIT,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_RECEIVED,
    ClaimNotFound,
    DuplicateClaim,
    InvalidTransition,
)
from claims_store_pg import PgClaimsStore


@pytest.fixture
def store():
    s = PgClaimsStore(database_url=TEST_DATABASE_URL)
    s._execute("TRUNCATE TABLE attachments, claim_history, claim_customer_notes, claims RESTART IDENTITY CASCADE")
    s._execute("TRUNCATE TABLE accounts")
    s._execute("TRUNCATE TABLE license_notification_logs, licenses, products, customers RESTART IDENTITY CASCADE")
    s._execute("TRUNCATE TABLE manuals RESTART IDENTITY CASCADE")
    s._conn.commit()
    return s


def test_license_lifecycle(store):
    from datetime import date, timedelta

    customer = store.create_customer("가나전자", "02-000-0000")
    product = store.create_product(customer["id"], "냉장고 K100")
    lic = store.create_license(
        product["id"], "Standard",
        (date.today() - timedelta(days=10)).isoformat(),
        (date.today() + timedelta(days=5)).isoformat(),
        "ops@company.com",
    )
    assert lic["customer_name"] == "가나전자"
    assert lic["status"] == "만료임박"

    expiring = store.list_expiring_licenses()
    assert len(expiring) == 1

    store.mark_license_notified(lic["id"])
    refreshed = store.list_licenses()[0]
    assert refreshed["last_notified_at"] != ""


def test_create_and_get_account(store):
    from auth import hash_password, verify_password

    account = store.create_account("pgadmin", hash_password("PgAdminPass123"), "admin", "관리자")
    assert "password_hash" not in account

    full = store.get_account("pgadmin")
    assert verify_password("PgAdminPass123", full["password_hash"])
    assert not verify_password("wrong", full["password_hash"])


def test_duplicate_account_rejected(store):
    from auth import hash_password

    store.create_account("dup1", hash_password("Password123"), "staff")
    with pytest.raises(ValueError):
        store.create_account("dup1", hash_password("Password123"), "staff")


def test_record_login_result_locks_after_max_attempts(store):
    from auth import hash_password

    store.create_account("locktest", hash_password("LockTestPass123"), "staff")
    for _ in range(5):
        store.record_login_result("locktest", success=False)

    account = store.get_account("locktest")
    assert account["locked_until"] != ""


def test_successful_login_resets_failed_attempts(store):
    from auth import hash_password

    store.create_account("resettest", hash_password("ResetTestPass123"), "staff")
    store.record_login_result("resettest", success=False)
    store.record_login_result("resettest", success=False)
    store.record_login_result("resettest", success=True)

    account = store.get_account("resettest")
    assert account["failed_attempts"] == 0
    assert account["locked_until"] == ""
    assert account["last_login_at"] != ""


def test_list_accounts_excludes_password_hash(store):
    from auth import hash_password

    store.create_account("listtest", hash_password("ListTestPass123"), "staff")
    for account in store.list_accounts():
        assert "password_hash" not in account


def test_create_claim_defaults_to_received(store):
    row = store.create_claim({"customer": "가나전자", "product": "냉장고", "description": "문제"})
    assert row["status"] == STATUS_RECEIVED
    assert row["claim_id"].startswith("CLM-")


def test_valid_and_invalid_transitions(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    updated = store.update_claim(row["claim_id"], {"status": STATUS_IN_PROGRESS})
    assert updated["status"] == STATUS_IN_PROGRESS

    store.update_claim(row["claim_id"], {"status": STATUS_DONE})
    with pytest.raises(InvalidTransition):
        store.update_claim(row["claim_id"], {"status": "존재안함"})


def test_reopen_after_done_is_allowed(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    store.update_claim(row["claim_id"], {"status": STATUS_DONE})
    reopened = store.update_claim(row["claim_id"], {"status": STATUS_IN_PROGRESS, "note": "재발"})
    assert reopened["status"] == STATUS_IN_PROGRESS
    assert reopened["completed_at"] == ""
    assert store.list_claim_history(row["claim_id"])[-1]["action_type"] == "reopen"


def test_import_claim_duplicate_detection(store):
    data = {
        "customer": "가나전자",
        "product": "냉장고",
        "description": "문이 안닫힘",
        "created_at": "2026-08-01 00:00:00",
        "status": STATUS_DONE,
    }
    store.import_claim(data)
    with pytest.raises(DuplicateClaim):
        store.import_claim(data)


def test_list_claims_assignee_partial_match_case_insensitive(store):
    a = store.create_claim({"customer": "A", "product": "P", "description": "D", "assignee": "Kim Sawon"})
    store.create_claim({"customer": "B", "product": "P", "description": "D", "assignee": "Lee"})
    results = store.list_claims(assignee="kim")
    assert len(results) == 1
    assert results[0]["claim_id"] == a["claim_id"]


def test_list_claims_filters_by_contact_ignoring_hyphens(store):
    a = store.create_claim({"customer": "A", "product": "P", "description": "D", "contact": "010-9876-5432"})
    store.create_claim({"customer": "B", "product": "P", "description": "D", "contact": "010-1111-2222"})
    results = store.list_claims(contact="98765432")
    assert len(results) == 1
    assert results[0]["claim_id"] == a["claim_id"]


def test_stats_summary_reflects_completed_this_month(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D", "cost_krw": 100})
    store.update_claim(row["claim_id"], {"status": STATUS_DONE})

    stats = store.stats_summary()
    assert stats["done"] == 1
    assert stats["this_month_count"] == 1
    assert stats["channel_breakdown"]["내부입력"] == 1


def test_claim_id_sequence_survives_reconnect(store):
    a = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    store._conn.close()  # 서버리스 웜 인스턴스에서 연결이 끊기는 상황을 흉내
    b = store.create_claim({"customer": "B", "product": "P", "description": "D"})
    seq_a = int(a["claim_id"].rsplit("-", 1)[-1])
    seq_b = int(b["claim_id"].rsplit("-", 1)[-1])
    assert seq_b == seq_a + 1


# ---------------------------------------------------------------- 확장 기능 (SQLite 버전과 동일 인터페이스 검증)

def _iso(days):
    from datetime import date, timedelta

    return (date.today() + timedelta(days=days)).isoformat()


def test_update_claim_records_processing_detail(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    updated = store.update_claim(row["claim_id"], {"processing_type": PROCESSING_ENGINEER_VISIT, "difficulty": DIFFICULTY_HIGH,
                                                   "sub_status": "엔지니어방문예정", "processing_minutes": 30})
    assert updated["processing_type"] == PROCESSING_ENGINEER_VISIT and updated["difficulty"] == DIFFICULTY_HIGH
    assert store.get_claim(row["claim_id"])["processing_minutes"] == 30
    with pytest.raises(ValueError):
        store.update_claim(row["claim_id"], {"processing_type": "텔레포트"})


def test_attachments_and_notes_and_history(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    att = store.add_attachment(row["claim_id"], "a.png", "stored.png", "image/png", 10)
    assert store.list_attachments(row["claim_id"])[0]["id"] == att["id"]
    assert store.delete_attachment(att["id"])["id"] == att["id"]
    with pytest.raises(ClaimNotFound):
        store.add_attachment("CLM-NOPE-001", "x.png", "y.png", "image/png", 10)

    store.add_customer_note(row["claim_id"], "사진 추가 첨부")
    assert store.list_customer_notes(row["claim_id"])[0]["body"] == "사진 추가 첨부"

    store.update_claim(row["claim_id"], {"status": STATUS_IN_PROGRESS, "changed_by": "kim"})
    history = store.list_claim_history(row["claim_id"])
    assert len(history) == 2 and history[-1]["changed_by"] == "kim"


def test_customer_product_license_crud_and_overview(store):
    customer = store.create_customer("가나전자", email="ops@gana.com")
    assert store.update_customer(customer["id"], {"memo": "VIP"})["memo"] == "VIP"
    product = store.create_product(customer["id"], "냉장고")
    store.update_product(product["id"], "냉장고 K200")
    lic = store.create_license(product["id"], "Standard", _iso(-10), _iso(10), "ops@a.com", warning_days=7, license_key="KEY-1")
    assert lic["warning_days"] == 7 and store.find_license_by_key("KEY-1") is not None
    store.create_claim({"customer": "가나전자", "product": "냉장고", "description": "D", "customer_id": customer["id"]})
    overview = store.get_customer_overview(customer["id"])
    assert overview["claim_count"] == 1 and len(overview["licenses"]) == 1

    store.update_license(lic["id"], {"notify_target_option": "staff"})
    store.log_license_notification(lic["id"], ["ops@a.com"], 5, True)
    assert store.list_license_notification_logs(lic["id"])[0]["success"] == 1
    store.delete_license(lic["id"])
    store.delete_product(product["id"])
    store.delete_customer(customer["id"])
    assert store.get_customer(customer["id"]) is None


def test_manual_crud_and_find_similar_claims(store):
    manual = store.create_manual("제목", "분류", "제품", "키워드", "내용")
    store.update_manual(manual["id"], {"content": "수정된 내용"})
    assert store.get_manual(manual["id"])["content"] == "수정된 내용"
    assert len(store.list_manuals(q="제목")) == 1
    store.delete_manual(manual["id"])

    old = store.create_claim({"customer": "X", "product": "P", "category": "C", "description": "D1"})
    store.update_claim(old["claim_id"], {"status": STATUS_IN_PROGRESS})
    store.update_claim(old["claim_id"], {"status": STATUS_DONE, "resolution": "해결함"})
    current = store.create_claim({"customer": "Y", "product": "P", "category": "C", "description": "D2"})
    similar = store.find_similar_claims(current["claim_id"])
    assert len(similar) == 1 and similar[0]["claim_id"] == old["claim_id"]


def test_account_management(store):
    from auth import hash_password

    store.create_account("acc1", hash_password("Password123"), "staff")
    assert store.set_account_active("acc1", False)["is_active"] == 0
    store.unlock_account("acc1")
    store.set_account_password("acc1", hash_password("NewPassword456"))
    with pytest.raises(ValueError):
        store.set_account_active("nobody", True)


def test_legacy_korean_difficulty_values_migrated_on_reconnect(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    store._execute("UPDATE claims SET difficulty='어려움' WHERE claim_id=%s", (row["claim_id"],))
    store._conn.commit()
    assert PgClaimsStore(database_url=TEST_DATABASE_URL).get_claim(row["claim_id"])["difficulty"] == "high"


def test_list_claims_filters_by_difficulty(store):
    low = store.create_claim({"customer": "A", "product": "P", "description": "D", "difficulty": "low"})
    store.create_claim({"customer": "B", "product": "P", "description": "D", "difficulty": "high"})
    results = store.list_claims(difficulty="low")
    assert len(results) == 1 and results[0]["claim_id"] == low["claim_id"]
