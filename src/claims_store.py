"""
클레임의 '현재 상태'를 관리하는 마스터 테이블 (data/claims.csv).

claim_logger.py 가 완료된 클레임의 이력을 일/월/년으로 쌓는 로그라면,
여기는 접수~처리중~완료 사이의 살아있는 상태(담당자, 상태값)를 다루는
가변 테이블이다. 완료로 전환되는 순간 claim_logger에 기록을 넘긴다.
"""

from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from claim_logger import ClaimLogger

STATUS_RECEIVED = "접수"
STATUS_IN_PROGRESS = "처리중"
STATUS_DONE = "완료"

VALID_STATUSES = {STATUS_RECEIVED, STATUS_IN_PROGRESS, STATUS_DONE}
VALID_TRANSITIONS = {
    STATUS_RECEIVED: {STATUS_IN_PROGRESS, STATUS_DONE},
    STATUS_IN_PROGRESS: {STATUS_DONE, STATUS_RECEIVED},
    STATUS_DONE: set(),  # 완료 후에는 상태 변경 불가 (재오픈이 필요하면 신규 클레임으로 등록)
}

CHANNEL_INTERNAL = "내부입력"
CHANNEL_WEB = "웹"
CHANNEL_APP = "앱"
CHANNEL_SMS = "문자"
CHANNEL_VOICE = "음성"

VALID_CHANNELS = {CHANNEL_INTERNAL, CHANNEL_WEB, CHANNEL_APP, CHANNEL_SMS, CHANNEL_VOICE}

FIELDS = [
    "claim_id",
    "created_at",
    "updated_at",
    "channel",
    "customer",
    "contact",
    "product",
    "category",
    "amount_krw",
    "description",
    "status",
    "assignee",
    "completed_at",
    "tokens_input",
    "tokens_output",
    "cost_krw",
]


class InvalidTransition(ValueError):
    pass


class ClaimNotFound(KeyError):
    pass


@dataclass
class ClaimsStore:
    base_dir: Path = field(default_factory=lambda: Path("data"))
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / "claims.csv"
        self.logger = ClaimLogger(base_dir=Path("logs"))
        if not self.path.exists():
            self._write_all([])

    # ------------------------------------------------------------------ #
    def create_claim(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rows = self._read_all()
            now = datetime.now()
            claim_id = self._next_claim_id(rows, now)
            channel = data.get("channel") or CHANNEL_INTERNAL
            if channel not in VALID_CHANNELS:
                raise ValueError(f"알 수 없는 접수 채널: {channel}")
            row = {
                "claim_id": claim_id,
                "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "channel": channel,
                "customer": data.get("customer", ""),
                "contact": data.get("contact", ""),
                "product": data.get("product", ""),
                "category": data.get("category", ""),
                "amount_krw": data.get("amount_krw", ""),
                "description": data.get("description", ""),
                "status": STATUS_RECEIVED,
                "assignee": data.get("assignee", ""),
                "completed_at": "",
                "tokens_input": data.get("tokens_input", ""),
                "tokens_output": data.get("tokens_output", ""),
                "cost_krw": data.get("cost_krw", ""),
            }
            rows.append(row)
            self._write_all(rows)
            return row

    def list_claims(
        self,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        channel: Optional[str] = None,
        q: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        rows = self._read_all()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if assignee:
            rows = [r for r in rows if r.get("assignee") == assignee]
        if channel:
            rows = [r for r in rows if r.get("channel") == channel]
        if q:
            q_lower = q.lower()
            rows = [
                r
                for r in rows
                if q_lower in (r.get("customer", "") + r.get("product", "") + r.get("description", "")).lower()
            ]
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return rows

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        rows = self._read_all()
        for r in rows:
            if r["claim_id"] == claim_id:
                return r
        raise ClaimNotFound(claim_id)

    def update_claim(self, claim_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rows = self._read_all()
            idx = next((i for i, r in enumerate(rows) if r["claim_id"] == claim_id), None)
            if idx is None:
                raise ClaimNotFound(claim_id)
            row = rows[idx]

            new_status = updates.get("status")
            if new_status is not None and new_status != row["status"]:
                if new_status not in VALID_STATUSES:
                    raise InvalidTransition(f"알 수 없는 상태: {new_status}")
                if new_status not in VALID_TRANSITIONS[row["status"]]:
                    raise InvalidTransition(f"{row['status']} -> {new_status} 전환 불가")
                row["status"] = new_status
                if new_status == STATUS_DONE:
                    row["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            for key in ("assignee", "category", "amount_krw", "description", "tokens_input", "tokens_output", "cost_krw"):
                if key in updates and updates[key] is not None:
                    row[key] = updates[key]

            row["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows[idx] = row
            self._write_all(rows)

            if row["status"] == STATUS_DONE:
                self._flush_to_logger(row)

            return row

    # ------------------------------------------------------------------ #
    def _flush_to_logger(self, row: dict[str, Any]) -> None:
        """완료된 클레임을 claim_logger의 daily/monthly/yearly 이력에 기록한다."""
        structured = {
            "customer": row.get("customer"),
            "product": row.get("product"),
            "category": row.get("category"),
            "assignee": row.get("assignee"),
            "channel": row.get("channel"),
        }
        self.logger.log_claim(
            {
                "claim_id": row["claim_id"],
                "channel": row.get("channel", ""),
                "customer": row.get("customer", ""),
                "product": row.get("product", ""),
                "category": row.get("category", ""),
                "amount_krw": row.get("amount_krw", ""),
                "stt_text": row.get("description", ""),
                "structured_json": structured,
                "tokens_input": row.get("tokens_input") or 0,
                "tokens_output": row.get("tokens_output") or 0,
                "cost_krw": row.get("cost_krw") or 0,
                "status": "완료",
            }
        )

    def _next_claim_id(self, rows: list[dict[str, Any]], now: datetime) -> str:
        prefix = f"CLM-{now.strftime('%Y%m%d')}-"
        today_seqs = [
            int(r["claim_id"].rsplit("-", 1)[-1])
            for r in rows
            if r["claim_id"].startswith(prefix) and r["claim_id"].rsplit("-", 1)[-1].isdigit()
        ]
        next_seq = (max(today_seqs) + 1) if today_seqs else 1
        return f"{prefix}{next_seq:03d}"

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        # 구 스키마(channel/contact 없음) 파일을 읽을 때를 대비한 하위호환 보정
        for row in rows:
            for key in FIELDS:
                row.setdefault(key, "")
            if not row["channel"]:
                row["channel"] = CHANNEL_INTERNAL
        return rows

    def _write_all(self, rows: list[dict[str, Any]]) -> None:
        with self.path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
