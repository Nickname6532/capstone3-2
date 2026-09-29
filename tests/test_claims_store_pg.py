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

from claims_store import STATUS_DONE, STATUS_IN_PROGRESS, STATUS_RECEIVED, DuplicateClaim, InvalidTransition
from claims_store_pg import PgClaimsStore


@pytest.fixture
def store():
    s = PgClaimsStore(database_url=TEST_DATABASE_URL)
    s._execute("TRUNCATE TABLE claims")
    s._conn.commit()
    return s


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
        store.update_claim(row["claim_id"], {"status": STATUS_IN_PROGRESS})


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
