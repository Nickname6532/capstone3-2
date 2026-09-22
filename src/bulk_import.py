"""
기업이 기존에 엑셀/CSV로 관리하던 클레임 이력을 일괄로 읽어들이는 파서.

파일의 헤더가 회사마다 제각각일 수 있어서, 자주 쓰이는 한글/영문 헤더 이름을
COLUMN_ALIASES로 매핑해 자동 인식한다. 매핑되지 않은 컬럼은 무시하고,
필수 컬럼(고객사/제품/내용)이 비어 있는 행은 실패로 처리해 사유와 함께 보고한다.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from openpyxl import load_workbook

REQUIRED_FIELDS = ("customer", "product", "description")

# 헤더 정규화(공백/특수문자 제거, 소문자화) 후 이 표에서 찾는다.
COLUMN_ALIASES: dict[str, str] = {
    # customer
    "고객사": "customer", "고객": "customer", "업체": "customer", "업체명": "customer",
    "회사명": "customer", "customer": "customer", "company": "customer",
    # contact
    "연락처": "contact", "전화번호": "contact", "휴대폰": "contact", "phone": "contact",
    "contact": "contact",
    # product
    "제품": "product", "제품명": "product", "모델": "product", "모델명": "product",
    "품목": "product", "product": "product", "model": "product",
    # category
    "원인": "category", "원인유형": "category", "유형": "category", "분류": "category",
    "category": "category", "type": "category",
    # amount
    "금액": "amount_krw", "금액원": "amount_krw", "배상금액": "amount_krw",
    "amount": "amount_krw", "amountkrw": "amount_krw",
    # description
    "내용": "description", "클레임내용": "description", "불편내용": "description",
    "상세내용": "description", "비고": "description", "description": "description",
    "detail": "description", "content": "description",
    # status
    "상태": "status", "처리상태": "status", "status": "status",
    # assignee
    "담당자": "assignee", "담당": "assignee", "처리자": "assignee", "assignee": "assignee",
    # channel
    "채널": "channel", "접수채널": "channel", "channel": "channel",
    # created_at
    "접수일": "created_at", "접수일자": "created_at", "접수일시": "created_at",
    "등록일": "created_at", "날짜": "created_at", "date": "created_at",
    "createdat": "created_at",
    # completed_at
    "완료일": "completed_at", "처리일": "completed_at", "완료일자": "completed_at",
    "completedat": "completed_at",
}

STATUS_ALIASES = {
    "접수": "접수", "접수완료": "접수", "대기": "접수", "미처리": "접수",
    "처리중": "처리중", "진행중": "처리중", "처리": "처리중",
    "완료": "완료", "처리완료": "완료", "완료됨": "완료", "종결": "완료",
}

DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d")


def _normalize_header(h: str) -> str:
    return "".join(str(h).split()).lower()


def _parse_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _map_row(raw: dict[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for key, value in raw.items():
        if key is None:
            continue
        field = COLUMN_ALIASES.get(_normalize_header(key))
        if not field or value is None or str(value).strip() == "":
            continue
        mapped[field] = str(value).strip()

    if "status" in mapped:
        mapped["status"] = STATUS_ALIASES.get(mapped["status"], None)
    if "created_at" in mapped:
        parsed = _parse_date(mapped["created_at"])
        if parsed:
            mapped["created_at"] = parsed
        else:
            mapped.pop("created_at")
    if "completed_at" in mapped:
        parsed = _parse_date(mapped["completed_at"])
        if parsed:
            mapped["completed_at"] = parsed
        else:
            mapped.pop("completed_at")
    if "amount_krw" in mapped:
        try:
            mapped["amount_krw"] = float(str(mapped["amount_krw"]).replace(",", ""))
        except ValueError:
            mapped.pop("amount_krw")
    return mapped


def parse_rows(filename: str, content: bytes) -> list[dict[str, Any]]:
    """CSV 또는 XLSX 바이트를 읽어 원시 딕셔너리(헤더→값) 리스트로 반환한다."""
    lower = filename.lower()
    if lower.endswith(".csv"):
        text = content.decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))
    if lower.endswith(".xlsx"):
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header = [str(h) if h is not None else "" for h in next(rows_iter)]
        out = []
        for values in rows_iter:
            if all(v is None or str(v).strip() == "" for v in values):
                continue
            out.append(dict(zip(header, values)))
        return out
    raise ValueError("지원하지 않는 파일 형식입니다 (.csv 또는 .xlsx만 가능)")


def import_rows(filename: str, content: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(성공적으로 매핑된 행 목록, 실패한 행과 사유 목록)을 반환한다."""
    raw_rows = parse_rows(filename, content)
    ok: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for i, raw in enumerate(raw_rows, start=2):  # 1행은 헤더이므로 데이터는 2행부터
        mapped = _map_row(raw)
        missing = [f for f in REQUIRED_FIELDS if not mapped.get(f)]
        if missing:
            failed.append({"row": i, "reason": f"필수 항목 누락: {', '.join(missing)}", "raw": raw})
            continue
        ok.append(mapped)

    return ok, failed
