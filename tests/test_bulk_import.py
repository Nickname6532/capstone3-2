import io

import pytest
from openpyxl import Workbook

import bulk_import


CSV_OK = (
    "고객사,제품,내용,상태,담당자,접수일\n"
    "가나전자,냉장고 K100,문이 잘 안닫힘,완료,박대리,2026-08-01\n"
    "다라산업,보일러 B200,점화가 안됨,처리중,,2026-08-15\n"
    "마바상사,,내용만 있고 제품이 없음,접수,,2026-08-20\n"
).encode("utf-8-sig")


def test_parse_csv_maps_korean_headers():
    rows = bulk_import.parse_rows("t.csv", CSV_OK)
    assert len(rows) == 3


def test_import_rows_separates_ok_and_failed():
    ok, dup, failed = bulk_import.import_rows("t.csv", CSV_OK)
    assert len(ok) == 2
    assert len(failed) == 1
    assert "product" in failed[0]["reason"]


def test_cp949_encoded_csv_is_read():
    text = "고객사,제품,내용\n사아전자,인버터,소음 발생\n"
    content = text.encode("cp949")
    ok, dup, failed = bulk_import.import_rows("t.csv", content)
    assert len(ok) == 1
    assert ok[0]["customer"] == "사아전자"


def test_intra_file_duplicate_detected():
    text = (
        "고객사,제품,내용\n"
        "중복테스트,모델A,같은 내용\n"
        "중복테스트,모델A,같은 내용\n"
    )
    ok, dup, failed = bulk_import.import_rows("t.csv", text.encode("utf-8-sig"))
    assert len(ok) == 1
    assert len(dup) == 1


def test_unknown_status_becomes_warning_not_failure():
    text = "고객사,제품,내용,상태\n가가,나나,다다,희한한상태\n"
    ok, dup, failed = bulk_import.import_rows("t.csv", text.encode("utf-8-sig"))
    assert len(ok) == 1
    assert "_warnings" in ok[0]
    assert not failed


def test_unsupported_extension_raises():
    with pytest.raises(ValueError):
        bulk_import.import_rows("t.txt", b"hello")


def test_empty_file_raises():
    with pytest.raises(ValueError):
        bulk_import.import_rows("t.csv", b"")


def test_corrupted_xlsx_raises_friendly_error():
    with pytest.raises(ValueError, match="엑셀 파일을 열 수 없습니다"):
        bulk_import.import_rows("t.xlsx", b"not a real xlsx file")


def test_row_count_over_limit_raises():
    lines = ["고객사,제품,내용"] + [f"c{i},p{i},d{i}" for i in range(bulk_import.MAX_ROWS + 1)]
    content = ("\n".join(lines) + "\n").encode("utf-8-sig")
    with pytest.raises(ValueError, match="최대"):
        bulk_import.import_rows("t.csv", content)


def test_xlsx_parses_correctly():
    wb = Workbook()
    ws = wb.active
    ws.append(["고객사", "제품", "내용", "상태"])
    ws.append(["자차물산", "펌프 P9", "누수 발생", "완료"])
    buf = io.BytesIO()
    wb.save(buf)

    ok, dup, failed = bulk_import.import_rows("t.xlsx", buf.getvalue())
    assert len(ok) == 1
    assert ok[0]["customer"] == "자차물산"
    assert ok[0]["status"] == "완료"


def test_xlsx_row_shorter_than_header_does_not_crash():
    wb = Workbook()
    ws = wb.active
    ws.append(["고객사", "제품", "내용", "비고"])
    ws.append(["자차물산", "펌프 P9", "누수 발생"])  # 마지막 컬럼 값 없음
    buf = io.BytesIO()
    wb.save(buf)

    ok, dup, failed = bulk_import.import_rows("t.xlsx", buf.getvalue())
    assert len(ok) == 1
