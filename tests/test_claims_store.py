import pytest

from claim_logger import ClaimLogger
from claims_store import (
    CHANNEL_WEB,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_RECEIVED,
    ClaimNotFound,
    ClaimsStore,
    DuplicateClaim,
    InvalidTransition,
)


@pytest.fixture
def store(tmp_path):
    logger = ClaimLogger(base_dir=tmp_path / "logs")
    return ClaimsStore(base_dir=tmp_path / "data", logger=logger)


def test_create_claim_defaults_to_received(store):
    row = store.create_claim({"customer": "가나전자", "product": "냉장고", "description": "문제"})
    assert row["status"] == STATUS_RECEIVED
    assert row["claim_id"].startswith("CLM-")
    assert row["channel"] == "내부입력"


def test_claim_id_sequence_increments_per_day(store):
    a = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    b = store.create_claim({"customer": "B", "product": "P", "description": "D"})
    seq_a = int(a["claim_id"].rsplit("-", 1)[-1])
    seq_b = int(b["claim_id"].rsplit("-", 1)[-1])
    assert seq_b == seq_a + 1


def test_valid_status_transition(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    updated = store.update_claim(row["claim_id"], {"status": STATUS_IN_PROGRESS})
    assert updated["status"] == STATUS_IN_PROGRESS


def test_invalid_status_transition_rejected(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    store.update_claim(row["claim_id"], {"status": STATUS_DONE})
    with pytest.raises(InvalidTransition):
        store.update_claim(row["claim_id"], {"status": STATUS_IN_PROGRESS})


def test_unknown_status_rejected(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    with pytest.raises(InvalidTransition):
        store.update_claim(row["claim_id"], {"status": "보류"})


def test_get_claim_not_found_raises(store):
    with pytest.raises(ClaimNotFound):
        store.get_claim("CLM-NOPE-000")


def test_invalid_channel_rejected(store):
    with pytest.raises(ValueError):
        store.create_claim({"customer": "A", "product": "P", "description": "D", "channel": "카카오톡"})


def test_completion_flushes_to_logger(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D", "channel": CHANNEL_WEB})
    store.update_claim(row["claim_id"], {"status": STATUS_DONE})
    daily_files = list((store.logger.daily_dir).glob("*.csv"))
    assert daily_files, "완료 시 일별 로그 파일이 생성되어야 한다"
    content = daily_files[0].read_text(encoding="utf-8-sig")
    assert row["claim_id"] in content


def test_list_claims_filters_by_assignee_partial_match(store):
    a = store.create_claim({"customer": "A", "product": "P", "description": "D", "assignee": "김사원"})
    store.create_claim({"customer": "B", "product": "P", "description": "D", "assignee": "이대리"})
    results = store.list_claims(assignee="사원")
    assert len(results) == 1
    assert results[0]["claim_id"] == a["claim_id"]


def test_import_claim_accepts_historical_status_and_date(store):
    row = store.import_claim(
        {
            "customer": "가나전자",
            "product": "냉장고",
            "description": "문이 안닫힘",
            "status": STATUS_DONE,
            "created_at": "2026-08-01 00:00:00",
        }
    )
    assert row["status"] == STATUS_DONE
    assert row["created_at"] == "2026-08-01 00:00:00"
    assert row["completed_at"] == "2026-08-01 00:00:00"

    daily_file = store.logger.daily_dir / "2026-08-01.csv"
    assert daily_file.exists(), "완료 이력은 원래 날짜의 일별 로그로 들어가야 한다"


def test_import_claim_detects_duplicate(store):
    data = {
        "customer": "가나전자",
        "product": "냉장고",
        "description": "문이 안닫힘",
        "created_at": "2026-08-01 00:00:00",
    }
    store.import_claim(data)
    with pytest.raises(DuplicateClaim):
        store.import_claim(data)
