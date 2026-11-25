import pytest

from claim_logger import ClaimLogger
from claims_store import (
    CHANNEL_WEB,
    DIFFICULTY_HIGH,
    PROCESSING_ENGINEER_VISIT,
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


def test_reopen_after_done_is_allowed_and_clears_completed_at(store):
    """완료 후에도 재발·추가조치 시 처리중으로 되돌릴 수 있어야 한다."""
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    store.update_claim(row["claim_id"], {"status": STATUS_DONE})
    reopened = store.update_claim(row["claim_id"], {"status": STATUS_IN_PROGRESS, "note": "재발 접수"})
    assert reopened["status"] == STATUS_IN_PROGRESS
    assert reopened["completed_at"] == ""

    history = store.list_claim_history(row["claim_id"])
    assert history[-1]["action_type"] == "reopen"
    assert history[-1]["note"] == "재발 접수"


def test_invalid_status_transition_rejected(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    with pytest.raises(InvalidTransition):
        store.update_claim(row["claim_id"], {"status": "존재안함"})


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


def test_list_claims_filters_by_contact_ignoring_hyphens(store):
    a = store.create_claim({"customer": "A", "product": "P", "description": "D", "contact": "010-9876-5432"})
    store.create_claim({"customer": "B", "product": "P", "description": "D", "contact": "010-1111-2222"})

    results_no_hyphen = store.list_claims(contact="98765432")
    assert len(results_no_hyphen) == 1
    assert results_no_hyphen[0]["claim_id"] == a["claim_id"]

    results_with_hyphen = store.list_claims(contact="010-9876")
    assert len(results_with_hyphen) == 1
    assert results_with_hyphen[0]["claim_id"] == a["claim_id"]


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


# ---------------------------------------------------------------- 처리상세 / 첨부파일

def test_update_claim_records_processing_detail(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    updated = store.update_claim(
        row["claim_id"],
        {
            "processing_type": PROCESSING_ENGINEER_VISIT,
            "visit_time": "2026-11-12 14:00",
            "resolution": "현장 점검 후 설정 초기화로 해결",
            "difficulty": DIFFICULTY_HIGH,
        },
    )
    assert updated["processing_type"] == PROCESSING_ENGINEER_VISIT
    assert updated["difficulty"] == DIFFICULTY_HIGH
    assert store.get_claim(row["claim_id"])["resolution"] == "현장 점검 후 설정 초기화로 해결"


def test_update_claim_rejects_unknown_processing_type(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    with pytest.raises(ValueError):
        store.update_claim(row["claim_id"], {"processing_type": "텔레포트"})


def test_update_claim_rejects_unknown_difficulty(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    with pytest.raises(ValueError):
        store.update_claim(row["claim_id"], {"difficulty": "불가능"})


def test_add_list_delete_attachments(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    att = store.add_attachment(row["claim_id"], "screenshot.png", "abc123.png", "image/png", 2048)
    assert store.list_attachments(row["claim_id"])[0]["stored_name"] == "abc123.png"
    assert store.get_attachment(att["id"])["content_type"] == "image/png"

    deleted = store.delete_attachment(att["id"])
    assert deleted["id"] == att["id"]
    assert store.get_attachment(att["id"]) is None
    assert store.delete_attachment(att["id"]) is None


def test_add_attachment_to_unknown_claim_raises(store):
    with pytest.raises(ClaimNotFound):
        store.add_attachment("CLM-NOPE-001", "x.png", "y.png", "image/png", 10)


# ---------------------------------------------------------------- 처리이력 / 고객 코멘트

def test_create_claim_logs_history(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    history = store.list_claim_history(row["claim_id"])
    assert len(history) == 1
    assert history[0]["action_type"] == "create"
    assert history[0]["new_status"] == STATUS_RECEIVED


def test_status_change_appends_history_entry(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    store.update_claim(row["claim_id"], {"status": STATUS_IN_PROGRESS, "changed_by": "kim", "note": "접수 확인"})
    history = store.list_claim_history(row["claim_id"])
    assert len(history) == 2
    assert history[-1]["prev_status"] == STATUS_RECEIVED
    assert history[-1]["new_status"] == STATUS_IN_PROGRESS
    assert history[-1]["changed_by"] == "kim"
    assert history[-1]["note"] == "접수 확인"


def test_add_and_list_customer_notes(store):
    row = store.create_claim({"customer": "A", "product": "P", "description": "D"})
    store.add_customer_note(row["claim_id"], "사진 추가로 첨부했습니다")
    notes = store.list_customer_notes(row["claim_id"])
    assert len(notes) == 1 and notes[0]["body"] == "사진 추가로 첨부했습니다"


def test_add_customer_note_to_unknown_claim_raises(store):
    with pytest.raises(ClaimNotFound):
        store.add_customer_note("CLM-NOPE-001", "x")


# ---------------------------------------------------------------- 매뉴얼 / 유사 클레임

def test_manual_crud(store):
    manual = store.create_manual("SSO 500 오류 대응", "로그인", "Pro 플랜", "SSO,500,로그인", "토큰 캐시를 지우고 재시도")
    updated = store.update_manual(manual["id"], {"content": "토큰 캐시 삭제 후 재로그인 안내"})
    assert updated["content"] == "토큰 캐시 삭제 후 재로그인 안내"
    assert len(store.list_manuals(q="SSO")) == 1
    store.delete_manual(manual["id"])
    assert store.get_manual(manual["id"]) is None


def test_find_similar_claims_prefers_same_product_and_category(store):
    old = store.create_claim({"customer": "X", "product": "Pro 플랜", "category": "로그인/인증 오류", "description": "D1"})
    store.update_claim(old["claim_id"], {"status": STATUS_IN_PROGRESS})
    store.update_claim(old["claim_id"], {"status": STATUS_DONE, "resolution": "캐시 삭제로 해결"})

    unrelated = store.create_claim({"customer": "Y", "product": "다른플랜", "category": "기타", "description": "D2"})
    store.update_claim(unrelated["claim_id"], {"status": STATUS_IN_PROGRESS})
    store.update_claim(unrelated["claim_id"], {"status": STATUS_DONE, "resolution": "무관한 해결"})

    current = store.create_claim({"customer": "Z", "product": "Pro 플랜", "category": "로그인/인증 오류", "description": "D3"})
    similar = store.find_similar_claims(current["claim_id"])
    assert len(similar) == 1
    assert similar[0]["claim_id"] == old["claim_id"]


def test_find_similar_claims_excludes_unresolved(store):
    unresolved = store.create_claim({"customer": "X", "product": "P", "category": "기타", "description": "D"})
    store.update_claim(unresolved["claim_id"], {"status": STATUS_IN_PROGRESS})
    store.update_claim(unresolved["claim_id"], {"status": STATUS_DONE})  # resolution 비어있음
    current = store.create_claim({"customer": "Y", "product": "P", "category": "기타", "description": "D2"})
    assert store.find_similar_claims(current["claim_id"]) == []


# ---------------------------------------------------------------- 난이도 체계(low/medium/high)

def test_legacy_korean_difficulty_values_migrated_to_english(tmp_path):
    """예전 쉬움/보통/어려움(한글) 값이 저장된 기존 DB를 다시 열면 low/medium/high로 바뀌어야 한다."""
    base_dir = tmp_path / "data"
    first = ClaimsStore(base_dir=base_dir, logger=ClaimLogger(base_dir=tmp_path / "logs"))
    row = first.create_claim({"customer": "A", "product": "P", "description": "D"})
    first._conn.execute("UPDATE claims SET difficulty='어려움' WHERE claim_id=?", (row["claim_id"],))
    first._conn.commit()

    reopened = ClaimsStore(base_dir=base_dir, logger=ClaimLogger(base_dir=tmp_path / "logs"))
    assert reopened.get_claim(row["claim_id"])["difficulty"] == DIFFICULTY_HIGH


def test_list_claims_filters_by_difficulty(store):
    low = store.create_claim({"customer": "A", "product": "P", "description": "D", "difficulty": "low"})
    store.create_claim({"customer": "B", "product": "P", "description": "D", "difficulty": "high"})
    results = store.list_claims(difficulty="low")
    assert len(results) == 1 and results[0]["claim_id"] == low["claim_id"]


def test_concurrent_reads_and_writes_do_not_corrupt_results(tmp_path):
    """화면 하나가 API를 여러 개 동시에 부르는 상황: 공유 SQLite 연결에서 읽기/쓰기가 섞여도 오류가 없어야 한다."""
    import threading

    from claim_logger import ClaimLogger
    from claims_store import ClaimsStore

    store = ClaimsStore(base_dir=tmp_path / "data", logger=ClaimLogger(base_dir=tmp_path / "logs"))
    for i in range(20):
        store.create_claim({"customer": f"c{i}", "product": "P", "description": "D", "assignee": "a"})
    errors: list[BaseException] = []

    def reader():
        try:
            for _ in range(150):
                rows = store.list_claims()
                assert rows and all("claim_id" in r for r in rows)
                store.list_customers()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def writer(n):
        try:
            for i in range(25):
                store.create_claim({"customer": f"w{n}-{i}", "product": "P", "description": "D", "assignee": "a"})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(6)] + [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:2]
    assert len(store.list_claims()) == 20 + 3 * 25
