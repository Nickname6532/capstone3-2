"""고객사·제품·라이선스 마스터 데이터를 엑셀/CSV로 일괄 등록하는 파서.

클레임 이력(bulk_import.py)과 달리 여기서 다루는 건 "누가 어떤 제품의 라이선스를
언제부터 언제까지 갖고 있는가"다. 회사마다 양식이 다를 수 있어 자주 쓰이는 헤더를
COLUMN_ALIASES로 매핑해 자동 인식하고(예: "사이트명"/"고객사", "라이선스 키",
"구독기간(년)" 등), 매핑되지 않은 컬럼은 무시한다.

파일 자체 파싱(형식/용량/인코딩)은 bulk_import.parse_rows를 그대로 재사용한다 —
CSV/XLSX를 원시 딕셔너리 행 리스트로 바꾸는 로직은 도메인과 무관하기 때문이다.
"""

from __future__ import annotations

from typing import Any

import bulk_import

REQUIRED_FIELDS = ("customer", "start_date", "end_date")

# 헤더 정규화(공백 제거) 후 이 표에서 찾는다. bulk_import._normalize_header와 동일한 규칙.
COLUMN_ALIASES: dict[str, str] = {
    "no": "_skip", "번호": "_skip", "no.": "_skip",
    "라이선스키": "license_key", "라이선스key": "license_key", "licensekey": "license_key", "키": "license_key",
    "사이트명": "customer", "사이트": "customer", "고객사": "customer", "고객사명": "customer",
    "customer": "customer", "site": "customer", "sitename": "customer", "회사명": "customer",
    "구분": "customer_category", "카테고리": "customer_category", "category": "customer_category",
    "업종": "customer_category",
    "제품": "product", "제품명": "product", "product": "product", "플랜": "product", "plan": "product",
    "플랜명": "product",
    "구독기간(년)": "plan_years", "구독기간": "plan_years", "기간": "plan_years", "년수": "plan_years",
    "구독년수": "plan_years", "years": "plan_years",
    "시작일": "start_date", "시작일자": "start_date", "startdate": "start_date", "start": "start_date",
    "만료일": "end_date", "만료일자": "end_date", "enddate": "end_date", "end": "end_date",
    "만료임박기준": "warning_days", "warningdays": "warning_days",
    "알림이메일": "notify_emails", "notifyemails": "notify_emails", "이메일": "notify_emails",
    "남은일수": "_skip", "daysleft": "_skip", "d-day": "_skip",
    "상태": "_skip", "status": "_skip",
}


def _normalize_header(h: str) -> str:
    return bulk_import._normalize_header(h)


def _map_row(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    mapped: dict[str, Any] = {}
    warnings: list[str] = []

    for key, value in raw.items():
        if key is None:
            continue
        field = COLUMN_ALIASES.get(_normalize_header(key))
        if not field or field == "_skip" or value is None or str(value).strip() == "":
            continue
        mapped[field] = str(value).strip()

    for date_field in ("start_date", "end_date"):
        if date_field in mapped:
            parsed = bulk_import._parse_date(mapped[date_field])
            if parsed:
                mapped[date_field] = parsed[:10]  # 라이선스는 날짜만 쓴다(시각 불필요)
            else:
                warnings.append(f"'{date_field}' 값 '{mapped[date_field]}'을 날짜로 인식하지 못해 이 행을 건너뜀")
                mapped.pop(date_field)

    if "plan_years" in mapped:
        cleaned = mapped["plan_years"].replace("년", "").strip()
        try:
            mapped["plan_years"] = int(float(cleaned))
        except ValueError:
            warnings.append(f"구독기간 '{mapped['plan_years']}'을 숫자로 인식하지 못해 무시함")
            mapped.pop("plan_years")

    if "warning_days" in mapped:
        try:
            mapped["warning_days"] = int(float(mapped["warning_days"]))
        except ValueError:
            mapped.pop("warning_days")

    return mapped, warnings


def looks_like_license_file(filename: str, content: bytes) -> bool:
    """헤더가 라이선스 양식(라이선스 키 또는 만료일+사이트명 컬럼)으로 보이는지 판별한다.
    클레임 가져오기에 라이선스 파일을 잘못 올렸을 때 안내하기 위함."""
    try:
        rows = bulk_import.parse_rows(filename, content)
    except ValueError:
        return False
    if not rows:
        return False
    fields = {COLUMN_ALIASES.get(_normalize_header(k)) for k in rows[0] if k}
    return "license_key" in fields or {"customer", "end_date"} <= fields


def import_rows(
    filename: str, content: bytes, default_product_name: str = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(등록할 행, 형식 문제로 실패한 행)을 반환한다.

    파일 자체가 문제면(형식/용량/손상/인코딩) bulk_import.parse_rows가 ValueError를
    던지고, 그대로 전파된다(호출자가 400으로 응답).
    """
    raw_rows = bulk_import.parse_rows(filename, content)
    ok: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for i, raw in enumerate(raw_rows, start=2):  # 1행은 헤더
        mapped, warnings = _map_row(raw)
        missing = [f for f in REQUIRED_FIELDS if not mapped.get(f)]
        if missing:
            failed.append({"row": i, "reason": f"필수 항목 누락: {', '.join(missing)}"})
            continue
        if mapped["end_date"] < mapped["start_date"]:
            failed.append({"row": i, "reason": "만료일이 시작일보다 빠릅니다"})
            continue

        if not mapped.get("product"):
            years = mapped.get("plan_years")
            mapped["product"] = default_product_name or (f"{years}년 플랜" if years else "기본 플랜")
        if not mapped.get("plan_name"):
            years = mapped.get("plan_years")
            mapped["plan_name"] = f"{years}년" if years else ""

        if warnings:
            mapped["_warnings"] = warnings
        ok.append(mapped)

    return ok, failed
