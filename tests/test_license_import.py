import io

from openpyxl import Workbook

import license_import

CSV_OK = (
    "No,라이선스 키,사이트명,구분,구독기간(년),시작일,만료일,남은 일수,상태\n"
    "1,JHNJOMXXO-2K1M9YRWC,삼선전자,코스피,5,2026-05-07,2031-05-07,1639,활성\n"
    "2,JO06I63PL-63HUV284Z,삼선전자,코스피,1,2026-05-18,2027-05-18,189,활성\n"
    "3,,누락고객없음,코스피,1,2026-05-18,,0,활성\n"
).encode("utf-8-sig")


def test_import_rows_maps_vmware_style_headers():
    ok, failed = license_import.import_rows("t.csv", CSV_OK)
    assert len(ok) == 2 and len(failed) == 1
    assert "누락" in failed[0]["reason"]
    row = ok[0]
    assert row["customer"] == "삼선전자" and row["customer_category"] == "코스피"
    assert row["license_key"] == "JHNJOMXXO-2K1M9YRWC"
    assert row["start_date"] == "2026-05-07" and row["end_date"] == "2031-05-07"
    assert row["plan_years"] == 5 and row["plan_name"] == "5년" and row["product"] == "5년 플랜"


def test_import_rows_uses_default_product_name():
    ok, _ = license_import.import_rows("t.csv", CSV_OK, default_product_name="VMware")
    assert ok[0]["product"] == "VMware"


def test_import_rows_rejects_end_before_start():
    csv_bytes = "사이트명,시작일,만료일\n업체,2026-05-18,2026-01-01\n".encode("utf-8-sig")
    ok, failed = license_import.import_rows("t.csv", csv_bytes)
    assert len(ok) == 0 and "빠릅니다" in failed[0]["reason"]


def test_import_rows_xlsx_matches_real_file_layout():
    wb = Workbook()
    ws = wb.active
    ws.append(["No", "라이선스 키", "사이트명", "구분", "구독기간(년)", "시작일", "만료일", "남은 일수", "상태"])
    ws.append([1, "ABC-123", "계몽대학교", "대학", 3, "2026-01-11", "2029-01-11", 793, "활성"])
    buf = io.BytesIO()
    wb.save(buf)
    ok, _ = license_import.import_rows("t.xlsx", buf.getvalue())
    assert len(ok) == 1 and ok[0]["customer"] == "계몽대학교" and ok[0]["license_key"] == "ABC-123"


def test_import_rows_with_explicit_product_column():
    csv_bytes = "사이트명,제품,시작일,만료일\n업체,VMware vSphere,2026-01-01,2027-01-01\n".encode("utf-8-sig")
    ok, _ = license_import.import_rows("t.csv", csv_bytes)
    assert ok[0]["product"] == "VMware vSphere"
