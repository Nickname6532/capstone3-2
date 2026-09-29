"""
클레임의 '현재 상태'를 관리하는 마스터 테이블 (SQLite: data/claims.db).

claim_logger.py 가 완료된 클레임의 이력을 일/월/년 CSV로 쌓는 리포팅용 로그라면,
여기는 접수~처리중~완료 사이의 살아있는 상태(담당자, 상태값)를 다루는 원본 데이터다.
CSV 파일은 여러 담당자가 동시에 접속하면 파일 전체를 다시 쓰는 구조라 쓰기 충돌
위험이 있고 서버리스 인스턴스 간에도 공유되지 않아, 실사용 단계에서 SQLite로
전환했다. 완료로 전환되는 순간 claim_logger에 기록을 넘기는 흐름은 그대로다.
"""

from __future__ import annotations

import csv
import sqlite3
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
CHANNEL_IMPORT = "일괄가져오기"

VALID_CHANNELS = {CHANNEL_INTERNAL, CHANNEL_WEB, CHANNEL_APP, CHANNEL_SMS, CHANNEL_VOICE, CHANNEL_IMPORT}

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

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    customer TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    amount_krw REAL NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    assignee TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    cost_krw REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_channel ON claims(channel);
"""


class InvalidTransition(ValueError):
    pass


class ClaimNotFound(KeyError):
    pass


class DuplicateClaim(ValueError):
    """import_claim이 DB에 이미 있는 것과 같은 내용을 발견했을 때."""

    def __init__(self, existing_claim_id: str):
        self.existing_claim_id = existing_claim_id
        super().__init__(f"이미 등록된 클레임과 동일한 내용입니다 ({existing_claim_id})")


@dataclass
class ClaimsStore:
    base_dir: Path = field(default_factory=lambda: Path("data"))
    logger: Optional[ClaimLogger] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / "claims.db"
        # base_dir 기준 상대경로("logs")는 실행 시 cwd에 따라 다른 곳을 가리킬 수 있어
        # 호출자가 넘겨주지 않으면 이 파일 위치를 기준으로 한 절대경로를 쓴다.
        if self.logger is None:
            self.logger = ClaimLogger(base_dir=Path(__file__).resolve().parent / "logs")

        legacy_csv = self.base_dir / "claims.csv"
        is_new_db = not self.path.exists()

        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        if is_new_db and legacy_csv.exists():
            self._migrate_from_csv(legacy_csv)

    # ------------------------------------------------------------------ #
    def create_claim(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            now = datetime.now()
            channel = data.get("channel") or CHANNEL_INTERNAL
            if channel not in VALID_CHANNELS:
                raise ValueError(f"알 수 없는 접수 채널: {channel}")
            claim_id = self._next_claim_id(now)
            row = {
                "claim_id": claim_id,
                "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "channel": channel,
                "customer": data.get("customer", ""),
                "contact": data.get("contact", ""),
                "product": data.get("product", ""),
                "category": data.get("category", ""),
                "amount_krw": float(data.get("amount_krw") or 0),
                "description": data.get("description", ""),
                "status": STATUS_RECEIVED,
                "assignee": data.get("assignee", ""),
                "completed_at": "",
                "tokens_input": int(data.get("tokens_input") or 0),
                "tokens_output": int(data.get("tokens_output") or 0),
                "cost_krw": float(data.get("cost_krw") or 0),
            }
            self._conn.execute(
                f"INSERT INTO claims ({', '.join(FIELDS)}) VALUES ({', '.join('?' for _ in FIELDS)})",
                [row[f] for f in FIELDS],
            )
            self._conn.commit()
            return row

    def find_duplicate(self, customer: str, product: str, description: str, created_at: str) -> Optional[str]:
        """같은 고객사·제품·내용·접수일을 가진 클레임이 이미 있으면 그 claim_id를 반환한다."""
        row = self._conn.execute(
            "SELECT claim_id FROM claims WHERE customer=? AND product=? AND description=? AND created_at=?",
            (customer, product, description, created_at),
        ).fetchone()
        return row["claim_id"] if row else None

    def import_claim(self, data: dict[str, Any], skip_duplicates: bool = True) -> dict[str, Any]:
        """엑셀/CSV로 갖고 있던 과거 클레임 이력을 그대로 가져와 등록한다.

        create_claim과 달리 status/created_at/completed_at을 파일 값 그대로
        받아들인다(정상 전이 검증을 건너뛴다) — 이미 종결된 과거 기록이기 때문.
        완료 상태로 들어오면 그 시점(완료일 우선, 없으면 접수일) 기준으로
        일/월/년 이력에도 반영한다. skip_duplicates가 True(기본값)면 DB에
        이미 동일한 내용(고객사/제품/내용/접수일)이 있을 때 DuplicateClaim을 던진다.
        """
        with self._lock:
            now = datetime.now()
            channel = data.get("channel") or CHANNEL_IMPORT
            if channel not in VALID_CHANNELS:
                raise ValueError(f"알 수 없는 접수 채널: {channel}")
            status = data.get("status") if data.get("status") in VALID_STATUSES else STATUS_RECEIVED
            customer = data.get("customer", "")
            product = data.get("product", "")
            description = data.get("description", "")
            created_at = data.get("created_at") or now.strftime("%Y-%m-%d %H:%M:%S")

            if skip_duplicates:
                existing = self.find_duplicate(customer, product, description, created_at)
                if existing:
                    raise DuplicateClaim(existing)
            completed_at = data.get("completed_at", "") if status == STATUS_DONE else ""
            if status == STATUS_DONE and not completed_at:
                completed_at = created_at

            claim_id = self._next_claim_id(now)
            row = {
                "claim_id": claim_id,
                "created_at": created_at,
                "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "channel": channel,
                "customer": customer,
                "contact": data.get("contact", ""),
                "product": product,
                "category": data.get("category", ""),
                "amount_krw": float(data.get("amount_krw") or 0),
                "description": description,
                "status": status,
                "assignee": data.get("assignee", ""),
                "completed_at": completed_at,
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_krw": 0.0,
            }
            self._conn.execute(
                f"INSERT INTO claims ({', '.join(FIELDS)}) VALUES ({', '.join('?' for _ in FIELDS)})",
                [row[f] for f in FIELDS],
            )
            self._conn.commit()

            if status == STATUS_DONE:
                when = datetime.strptime(completed_at, "%Y-%m-%d %H:%M:%S")
                self._flush_to_logger(row, when=when)

            return row

    def list_claims(
        self,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        channel: Optional[str] = None,
        contact: Optional[str] = None,
        q: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if assignee:
            clauses.append("assignee LIKE ?")
            params.append(f"%{assignee}%")
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if contact:
            # 하이픈 유무와 상관없이 찾을 수 있도록 저장값·검색어 둘 다 숫자만 남겨 비교한다.
            clauses.append("REPLACE(contact, '-', '') LIKE ?")
            digits_only = "".join(ch for ch in contact if ch.isdigit())
            params.append(f"%{digits_only}%")
        if q:
            clauses.append("(customer LIKE ? OR product LIKE ? OR description LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])

        sql = "SELECT * FROM claims"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"

        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
        if row is None:
            raise ClaimNotFound(claim_id)
        return dict(row)

    def stats_summary(self) -> dict[str, Any]:
        """대시보드 상단 통계. 이번달 처리건수·비용은 claim_logger가 쌓은 월별 CSV에서 읽는다."""
        claims = self.list_claims()
        open_count = sum(1 for c in claims if c["status"] != STATUS_DONE)
        in_progress = sum(1 for c in claims if c["status"] == STATUS_IN_PROGRESS)
        received = sum(1 for c in claims if c["status"] == STATUS_RECEIVED)
        done = sum(1 for c in claims if c["status"] == STATUS_DONE)

        monthly_rows: list[dict[str, Any]] = []
        monthly_path = self.logger.monthly_dir
        if monthly_path.exists():
            files = sorted(monthly_path.glob("*_summary.csv"), reverse=True)
            if files:
                with files[0].open("r", newline="", encoding="utf-8-sig") as f:
                    monthly_rows = list(csv.DictReader(f))

        this_month_count = sum(int(r.get("claim_count") or 0) for r in monthly_rows)
        this_month_cost = sum(float(r.get("total_cost_krw") or 0) for r in monthly_rows)

        channel_breakdown: dict[str, int] = {}
        for c in claims:
            ch = c.get("channel") or CHANNEL_INTERNAL
            channel_breakdown[ch] = channel_breakdown.get(ch, 0) + 1

        return {
            "received": received,
            "in_progress": in_progress,
            "done": done,
            "open_total": open_count,
            "this_month_count": this_month_count,
            "this_month_cost_krw": round(this_month_cost, 1),
            "channel_breakdown": channel_breakdown,
        }

    def update_claim(self, claim_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self._conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
            if current is None:
                raise ClaimNotFound(claim_id)
            row = dict(current)

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

            self._conn.execute(
                """UPDATE claims SET updated_at=?, status=?, assignee=?, category=?, amount_krw=?,
                   description=?, completed_at=?, tokens_input=?, tokens_output=?, cost_krw=?
                   WHERE claim_id=?""",
                (
                    row["updated_at"],
                    row["status"],
                    row["assignee"],
                    row["category"],
                    row["amount_krw"],
                    row["description"],
                    row["completed_at"],
                    row["tokens_input"],
                    row["tokens_output"],
                    row["cost_krw"],
                    claim_id,
                ),
            )
            self._conn.commit()

            if row["status"] == STATUS_DONE:
                self._flush_to_logger(row)

            return row

    # ------------------------------------------------------------------ #
    def _flush_to_logger(self, row: dict[str, Any], when: Optional[datetime] = None) -> None:
        """완료된 클레임을 claim_logger의 daily/monthly/yearly 이력에 기록한다.

        when을 지정하면 그 날짜의 이력으로 쌓인다(과거 데이터 일괄 가져오기용).
        """
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
            },
            when=when,
        )

    def _next_claim_id(self, now: datetime) -> str:
        prefix = f"CLM-{now.strftime('%Y%m%d')}-"
        rows = self._conn.execute(
            "SELECT claim_id FROM claims WHERE claim_id LIKE ?", (f"{prefix}%",)
        ).fetchall()
        seqs = [
            int(r["claim_id"].rsplit("-", 1)[-1])
            for r in rows
            if r["claim_id"].rsplit("-", 1)[-1].isdigit()
        ]
        next_seq = (max(seqs) + 1) if seqs else 1
        return f"{prefix}{next_seq:03d}"

    def _migrate_from_csv(self, legacy_csv: Path) -> None:
        """이전 CSV 마스터 테이블(data/claims.csv)이 있으면 최초 1회 SQLite로 옮긴다."""
        with legacy_csv.open("r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return
        for r in rows:
            for key in FIELDS:
                r.setdefault(key, "")
            r["channel"] = r["channel"] or CHANNEL_INTERNAL
            r["amount_krw"] = float(r["amount_krw"] or 0)
            r["cost_krw"] = float(r["cost_krw"] or 0)
            r["tokens_input"] = int(float(r["tokens_input"])) if r["tokens_input"] else 0
            r["tokens_output"] = int(float(r["tokens_output"])) if r["tokens_output"] else 0
        self._conn.executemany(
            f"INSERT OR IGNORE INTO claims ({', '.join(FIELDS)}) VALUES ({', '.join('?' for _ in FIELDS)})",
            [[r[f] for f in FIELDS] for r in rows],
        )
        self._conn.commit()
        legacy_csv.rename(legacy_csv.with_suffix(".csv.migrated"))
