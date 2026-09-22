"""
클레임 처리 결과를 일별/월별/연별 CSV로 기록하는 로깅 모듈.

디렉터리 구조:
    logs/
      daily/2026-09-22.csv          <- 클레임 건별 원본 기록 (append)
      monthly/2026-09_summary.csv   <- 그 달의 일별 집계 1줄씩 (해당 일 처리 후 갱신)
      yearly/2026_summary.csv       <- 그 해의 월별 집계 1줄씩 (해당 월 처리 후 갱신)

사용 흐름:
    logger = ClaimLogger()
    logger.log_claim({
        "claim_id": "CLM-20260922-001",
        "customer": "OO전자",
        "product": "3상 인버터 모듈",
        "category": "부품불량",
        "amount_krw": 1200000,
        "stt_text": "...",
        "structured_json": {...},
        "tokens_input": 3800,
        "tokens_output": 400,
        "cost_krw": 24,
        "status": "OK",
    })
    # 위 호출이 daily CSV에 append하고, monthly/yearly 집계를 자동 갱신한다.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


DAILY_FIELDS = [
    "timestamp",
    "claim_id",
    "channel",
    "customer",
    "product",
    "category",
    "amount_krw",
    "stt_text",
    "structured_json",
    "tokens_input",
    "tokens_output",
    "tokens_total",
    "cost_krw",
    "status",
]

MONTHLY_FIELDS = [
    "date",
    "claim_count",
    "total_tokens",
    "total_cost_krw",
    "avg_cost_krw",
    "top_category",
    "category_breakdown",
]

YEARLY_FIELDS = [
    "month",
    "claim_count",
    "total_tokens",
    "total_cost_krw",
    "avg_cost_krw",
    "top_category",
    "category_breakdown",
]


@dataclass
class ClaimLogger:
    base_dir: Path = field(default_factory=lambda: Path("logs"))

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        self.daily_dir = self.base_dir / "daily"
        self.monthly_dir = self.base_dir / "monthly"
        self.yearly_dir = self.base_dir / "yearly"
        for d in (self.daily_dir, self.monthly_dir, self.yearly_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- #
    # 공개 API
    # ---------------------------------------------------------------- #
    def log_claim(self, record: dict[str, Any], when: datetime | None = None) -> None:
        """클레임 1건을 기록하고, 소속된 월/년 집계를 갱신한다."""
        now = when or datetime.now()
        row = self._build_daily_row(record, now)
        self._append_daily(row, now)
        self._refresh_monthly(now.year, now.month)
        self._refresh_yearly(now.year)

    # ---------------------------------------------------------------- #
    # 내부 구현
    # ---------------------------------------------------------------- #
    def _build_daily_row(self, record: dict[str, Any], now: datetime) -> dict[str, Any]:
        tokens_in = int(record.get("tokens_input", 0) or 0)
        tokens_out = int(record.get("tokens_output", 0) or 0)
        structured = record.get("structured_json", "")
        if isinstance(structured, (dict, list)):
            structured = json.dumps(structured, ensure_ascii=False)
        return {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "claim_id": record.get("claim_id", ""),
            "channel": record.get("channel", ""),
            "customer": record.get("customer", ""),
            "product": record.get("product", ""),
            "category": record.get("category", ""),
            "amount_krw": record.get("amount_krw", ""),
            "stt_text": record.get("stt_text", ""),
            "structured_json": structured,
            "tokens_input": tokens_in,
            "tokens_output": tokens_out,
            "tokens_total": tokens_in + tokens_out,
            "cost_krw": record.get("cost_krw", ""),
            "status": record.get("status", "OK"),
        }

    def _daily_path(self, now: datetime) -> Path:
        return self.daily_dir / f"{now.strftime('%Y-%m-%d')}.csv"

    def _monthly_path(self, year: int, month: int) -> Path:
        return self.monthly_dir / f"{year:04d}-{month:02d}_summary.csv"

    def _yearly_path(self, year: int) -> Path:
        return self.yearly_dir / f"{year:04d}_summary.csv"

    def _append_daily(self, row: dict[str, Any], now: datetime) -> None:
        path = self._daily_path(now)
        if path.exists():
            self._migrate_schema(path, DAILY_FIELDS)
        is_new = not path.exists()
        with path.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=DAILY_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)

    def _migrate_schema(self, path: Path, fields: list[str]) -> None:
        """이전 버전의 헤더(컬럼 추가 전)로 저장된 파일을 새 스키마로 맞춰 다시 쓴다."""
        existing_rows = self._read_rows(path)
        if existing_rows and list(existing_rows[0].keys()) == fields:
            return
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in existing_rows:
                for key in fields:
                    r.setdefault(key, "")
                writer.writerow({k: r.get(k, "") for k in fields})

    def _read_rows(self, path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def _summarize(self, rows: list[dict[str, str]]) -> dict[str, Any]:
        count = len(rows)
        total_tokens = sum(int(r.get("tokens_total") or 0) for r in rows)
        total_cost = sum(float(r.get("cost_krw") or 0) for r in rows)
        avg_cost = round(total_cost / count, 1) if count else 0

        breakdown: dict[str, int] = {}
        for r in rows:
            cat = r.get("category") or "미분류"
            breakdown[cat] = breakdown.get(cat, 0) + 1
        top_category = max(breakdown, key=breakdown.get) if breakdown else ""

        return {
            "claim_count": count,
            "total_tokens": total_tokens,
            "total_cost_krw": round(total_cost, 1),
            "avg_cost_krw": avg_cost,
            "top_category": top_category,
            "category_breakdown": json.dumps(breakdown, ensure_ascii=False),
        }

    def _refresh_monthly(self, year: int, month: int) -> None:
        """그 달에 속한 모든 daily CSV를 다시 읽어 일자별 요약 1줄씩으로 재작성한다."""
        prefix = f"{year:04d}-{month:02d}-"
        day_files = sorted(self.daily_dir.glob(f"{prefix}*.csv"))

        out_rows = []
        for day_file in day_files:
            date_str = day_file.stem  # YYYY-MM-DD
            rows = self._read_rows(day_file)
            summary = self._summarize(rows)
            out_rows.append({"date": date_str, **summary})

        path = self._monthly_path(year, month)
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=MONTHLY_FIELDS)
            writer.writeheader()
            writer.writerows(out_rows)

    def _refresh_yearly(self, year: int) -> None:
        """그 해에 속한 모든 monthly summary를 다시 읽어 월별 요약 1줄씩으로 재작성한다."""
        prefix = f"{year:04d}-"
        month_files = sorted(self.monthly_dir.glob(f"{prefix}*_summary.csv"))

        out_rows = []
        for month_file in month_files:
            month_str = month_file.stem.replace("_summary", "")  # YYYY-MM
            rows = self._read_rows(month_file)
            # monthly summary의 각 행은 "그 달의 하루" 요약이므로, 합산해서 그 달 전체 집계를 만든다.
            count = sum(int(r.get("claim_count") or 0) for r in rows)
            total_tokens = sum(int(r.get("total_tokens") or 0) for r in rows)
            total_cost = sum(float(r.get("total_cost_krw") or 0) for r in rows)
            avg_cost = round(total_cost / count, 1) if count else 0

            breakdown: dict[str, int] = {}
            for r in rows:
                try:
                    day_breakdown = json.loads(r.get("category_breakdown") or "{}")
                except json.JSONDecodeError:
                    day_breakdown = {}
                for cat, n in day_breakdown.items():
                    breakdown[cat] = breakdown.get(cat, 0) + n
            top_category = max(breakdown, key=breakdown.get) if breakdown else ""

            out_rows.append({
                "month": month_str,
                "claim_count": count,
                "total_tokens": total_tokens,
                "total_cost_krw": round(total_cost, 1),
                "avg_cost_krw": avg_cost,
                "top_category": top_category,
                "category_breakdown": json.dumps(breakdown, ensure_ascii=False),
            })

        path = self._yearly_path(year)
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=YEARLY_FIELDS)
            writer.writeheader()
            writer.writerows(out_rows)
