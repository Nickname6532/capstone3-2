"""
기업이 기존에 엑셀/CSV로 관리하던 클레임 이력을 일괄로 읽어들이는 파서.

파일의 헤더가 회사마다 제각각일 수 있어서, 자주 쓰이는 한글/영문 헤더 이름을
COLUMN_ALIASES로 매핑해 자동 인식한다. 매핑되지 않은 컬럼은 무시하고,
필수 컬럼(고객사/제품/내용)이 비어 있는 행은 실패로 처리해 사유와 함께 보고한다.

예외 처리 범위:
- 파일 자체 문제(형식/용량/행수/손상/인코딩)는 parse_rows에서 ValueError로 터뜨려
  API 쪽에서 400으로 응답한다.
- 행 단위 문제(필수값 누락, 파일 안에서의 중복)는 import_rows가 실패/중복 목록으로
  분류해 반환한다. DB에 이미 있는 데이터와의 중복은 claims_store 쪽에서 한 번 더
  걸러진다(파일 하나만 봐서는 알 수 없으므로).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from openpyxl import load_workbook

REQUIRED_FIELDS = ("customer", "product", "description")

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5MB
MAX_ROWS = 5000

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

VALID_CHANNELS_FROM_FILE = {"내부입력", "웹", "앱", "문자", "음성", "일괄가져오기"}

DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d")

CSV_ENCODINGS = ("utf-8-sig", "cp949", "euc-kr")


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


def _map_row(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """원시 행을 내부 필드명으로 매핑한다. (매핑된 값, 형식이 이상해 버린 값에 대한 경고 목록)."""
    mapped: dict[str, Any] = {}
    warnings: list[str] = []

    for key, value in raw.items():
        if key is None:
            continue
        field = COLUMN_ALIASES.get(_normalize_header(key))
        if not field or value is None or str(value).strip() == "":
            continue
        mapped[field] = str(value).strip()

    if "status" in mapped:
        normalized = STATUS_ALIASES.get(mapped["status"])
        if normalized:
            mapped["status"] = normalized
        else:
            warnings.append(f"알 수 없는 상태값 '{mapped['status']}' → 접수로 처리")
            mapped.pop("status")

    if "channel" in mapped and mapped["channel"] not in VALID_CHANNELS_FROM_FILE:
        warnings.append(f"알 수 없는 채널 '{mapped['channel']}' → 일괄가져오기로 처리")
        mapped.pop("channel")

    for date_field in ("created_at", "completed_at"):
        if date_field in mapped:
            parsed = _parse_date(mapped[date_field])
            if parsed:
                mapped[date_field] = parsed
            else:
                warnings.append(f"'{date_field}' 값 '{mapped[date_field]}'을 날짜로 인식하지 못해 무시함")
                mapped.pop(date_field)

    if "amount_krw" in mapped:
        cleaned = str(mapped["amount_krw"]).replace(",", "").replace("원", "").strip()
        try:
            mapped["amount_krw"] = float(cleaned)
        except ValueError:
            warnings.append(f"금액 '{mapped['amount_krw']}'을 숫자로 인식하지 못해 0으로 처리")
            mapped.pop("amount_krw")

    return mapped, warnings


def _decode_csv_bytes(content: bytes) -> str:
    last_error: Exception | None = None
    for enc in CSV_ENCODINGS:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError) as e:
            last_error = e
            continue
    raise ValueError(
        "파일 인코딩을 읽을 수 없습니다. UTF-8 또는 CP949(엑셀 기본 CSV 저장 형식)로 저장해주세요."
    ) from last_error


def parse_rows(filename: str, content: bytes) -> list[dict[str, Any]]:
    """CSV 또는 XLSX 바이트를 읽어 원시 딕셔너리(헤더→값) 리스트로 반환한다.

    파일 자체가 문제(형식/용량/손상/인코딩/행 수 초과)인 경우 ValueError를 던진다.
    """
    if not filename:
        raise ValueError("파일명이 없습니다.")
    if not content:
        raise ValueError("빈 파일입니다.")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"파일이 너무 큽니다 ({MAX_FILE_BYTES // (1024 * 1024)}MB 이하만 가능).")

    lower = filename.lower()

    if lower.endswith(".csv"):
        text = _decode_csv_bytes(content)
        try:
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
        except csv.Error as e:
            raise ValueError(f"CSV 형식을 읽을 수 없습니다: {e}") from e
        if reader.fieldnames is None or not any(f for f in reader.fieldnames if f):
            raise ValueError("헤더(첫 행)를 찾을 수 없습니다.")

    elif lower.endswith(".xlsx"):
        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            header_row = next(rows_iter)
        except StopIteration:
            raise ValueError("빈 시트입니다.")
        except Exception as e:  # openpyxl은 손상 파일에서 다양한 예외를 던진다
            raise ValueError(f"엑셀 파일을 열 수 없습니다. 손상되었거나 지원하지 않는 형식입니다: {e}") from e

        header = [str(h).strip() if h is not None else "" for h in header_row]
        if not any(header):
            raise ValueError("헤더(첫 행)를 찾을 수 없습니다.")

        rows = []
        for values in rows_iter:
            if all(v is None or str(v).strip() == "" for v in values):
                continue
            # 헤더보다 값이 적거나 많아도(수식/서식이 깨진 셀 등) 안전하게 맞춰 담는다.
            row = {}
            for idx, h in enumerate(header):
                if not h:
                    continue
                row[h] = values[idx] if idx < len(values) else None
            rows.append(row)
    else:
        raise ValueError("지원하지 않는 파일 형식입니다 (.csv 또는 .xlsx만 가능).")

    if len(rows) > MAX_ROWS:
        raise ValueError(f"한 번에 최대 {MAX_ROWS:,}행까지만 가져올 수 있습니다 (파일: {len(rows):,}행).")

    return rows


def _dedup_key(mapped: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        mapped.get("customer", ""),
        mapped.get("product", ""),
        mapped.get("description", ""),
        mapped.get("created_at", ""),
    )


def import_rows(
    filename: str, content: bytes
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(등록할 행, 파일 내부 중복이라 건너뛴 행, 형식 문제로 실패한 행)을 반환한다."""
    raw_rows = parse_rows(filename, content)
    ok: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str, str], int] = {}

    for i, raw in enumerate(raw_rows, start=2):  # 1행은 헤더이므로 데이터는 2행부터
        mapped, warnings = _map_row(raw)
        missing = [f for f in REQUIRED_FIELDS if not mapped.get(f)]
        if missing:
            failed.append({"row": i, "reason": f"필수 항목 누락: {', '.join(missing)}"})
            continue

        key = _dedup_key(mapped)
        if key in seen:
            duplicates.append({"row": i, "reason": f"{seen[key]}행과 동일한 내용 (파일 내 중복)"})
            continue
        seen[key] = i

        if warnings:
            mapped["_warnings"] = warnings
        ok.append(mapped)

    return ok, duplicates, failed
