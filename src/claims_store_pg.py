"""
클레임 마스터 테이블의 PostgreSQL 백엔드 (Vercel 등 서버리스 배포에서 영속성 확보용).

SQLite 버전(claims_store.ClaimsStore)과 퍼블릭 인터페이스가 동일해서 app.py는
DATABASE_URL 환경변수 유무만 보고 둘 중 하나를 고르면 된다. Vercel의 서버리스
인스턴스는 로컬 디스크(/tmp)가 인스턴스마다 다르고 언제든 초기화되기 때문에,
완료 이력의 일/월/년 집계도 CSV 파일이 아니라 claims 테이블에서 직접 SQL로
집계한다 — 그래야 인스턴스가 바뀌어도 통계가 그대로 유지된다.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row

# libpq(=psycopg)가 실제로 알아듣는 연결 파라미터만 통과시킨다. Vercel의 Supabase
# 연동이 넣어주는 URL에는 "supa" 같은 자체 메타데이터 쿼리 파라미터가 섞여 있는데,
# psycopg는 모르는 파라미터가 하나라도 있으면 URI 파싱 단계에서 바로 예외를 던진다
# (실제로 "invalid URI query parameter: 'supa'"로 배포가 통째로 죽었다).
_ALLOWED_QUERY_KEYS = {
    "sslmode", "sslrootcert", "sslcert", "sslkey", "sslpassword",
    "application_name", "connect_timeout", "options",
    "target_session_attrs", "channel_binding", "gssencmode",
    "keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count",
}


def _sanitize_conninfo(url: str) -> str:
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() in _ALLOWED_QUERY_KEYS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))

from claims_store import (
    CHANNEL_IMPORT,
    CHANNEL_INTERNAL,
    FIELDS,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_RECEIVED,
    VALID_CHANNELS,
    VALID_STATUSES,
    VALID_TRANSITIONS,
    ClaimNotFound,
    DuplicateClaim,
    InvalidTransition,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    customer TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    amount_krw DOUBLE PRECISION NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    assignee TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    cost_krw DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_channel ON claims(channel);
"""


class PgClaimsStore:
    def __init__(self, database_url: str):
        self.database_url = _sanitize_conninfo(database_url)
        self._lock = threading.Lock()
        self._conn: Optional[psycopg.Connection] = None
        self._ensure_conn()
        with self._conn.cursor() as cur:
            cur.execute(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    def _ensure_conn(self) -> psycopg.Connection:
        """연결이 끊겼으면(서버리스 웜 인스턴스 재사용 중 idle timeout 등) 새로 연다."""
        if self._conn is None or self._conn.closed:
            # prepare_threshold=None: Supabase/PgBouncer의 커넥션 풀러(트랜잭션 모드)는
            # 서버사이드 prepared statement를 커넥션 간에 유지하지 않아서, 기본값대로 두면
            # "prepared statement already exists" 류 오류가 날 수 있어 아예 끈다.
            self._conn = psycopg.connect(
                self.database_url, row_factory=dict_row, autocommit=False, prepare_threshold=None
            )
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> psycopg.Cursor:
        conn = self._ensure_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur
        except psycopg.OperationalError:
            self._conn = None
            conn = self._ensure_conn()
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur

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
            placeholders = ", ".join(f"%({f})s" for f in FIELDS)
            self._execute(f"INSERT INTO claims ({', '.join(FIELDS)}) VALUES ({placeholders})", row)
            self._conn.commit()
            return row

    def find_duplicate(self, customer: str, product: str, description: str, created_at: str) -> Optional[str]:
        cur = self._execute(
            "SELECT claim_id FROM claims WHERE customer=%s AND product=%s AND description=%s AND created_at=%s",
            (customer, product, description, created_at),
        )
        row = cur.fetchone()
        return row["claim_id"] if row else None

    def import_claim(self, data: dict[str, Any], skip_duplicates: bool = True) -> dict[str, Any]:
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
            placeholders = ", ".join(f"%({f})s" for f in FIELDS)
            self._execute(f"INSERT INTO claims ({', '.join(FIELDS)}) VALUES ({placeholders})", row)
            self._conn.commit()
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
            clauses.append("status = %s")
            params.append(status)
        if assignee:
            clauses.append("assignee ILIKE %s")
            params.append(f"%{assignee}%")
        if channel:
            clauses.append("channel = %s")
            params.append(channel)
        if contact:
            clauses.append("REPLACE(contact, '-', '') LIKE %s")
            digits_only = "".join(ch for ch in contact if ch.isdigit())
            params.append(f"%{digits_only}%")
        if q:
            clauses.append("(customer ILIKE %s OR product ILIKE %s OR description ILIKE %s)")
            like = f"%{q}%"
            params.extend([like, like, like])

        sql = "SELECT * FROM claims"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"

        cur = self._execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        cur = self._execute("SELECT * FROM claims WHERE claim_id = %s", (claim_id,))
        row = cur.fetchone()
        if row is None:
            raise ClaimNotFound(claim_id)
        return dict(row)

    def update_claim(self, claim_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cur = self._execute("SELECT * FROM claims WHERE claim_id = %s", (claim_id,))
            current = cur.fetchone()
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

            self._execute(
                """UPDATE claims SET updated_at=%s, status=%s, assignee=%s, category=%s, amount_krw=%s,
                   description=%s, completed_at=%s, tokens_input=%s, tokens_output=%s, cost_krw=%s
                   WHERE claim_id=%s""",
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
            return row

    def stats_summary(self) -> dict[str, Any]:
        """claims 테이블에서 직접 집계한다 — /tmp CSV에 의존하지 않아 인스턴스가 바뀌어도 값이 유지된다."""
        cur = self._execute("SELECT status, COUNT(*) AS n FROM claims GROUP BY status")
        status_counts = {r["status"]: r["n"] for r in cur.fetchall()}

        cur = self._execute("SELECT channel, COUNT(*) AS n FROM claims GROUP BY channel")
        channel_breakdown = {r["channel"]: r["n"] for r in cur.fetchall()}

        month_prefix = datetime.now().strftime("%Y-%m")
        cur = self._execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_krw), 0) AS cost FROM claims "
            "WHERE status = %s AND completed_at LIKE %s",
            (STATUS_DONE, f"{month_prefix}%"),
        )
        month_row = cur.fetchone()

        received = status_counts.get(STATUS_RECEIVED, 0)
        in_progress = status_counts.get(STATUS_IN_PROGRESS, 0)
        done = status_counts.get(STATUS_DONE, 0)

        return {
            "received": received,
            "in_progress": in_progress,
            "done": done,
            "open_total": received + in_progress,
            "this_month_count": month_row["n"],
            "this_month_cost_krw": round(float(month_row["cost"]), 1),
            "channel_breakdown": channel_breakdown,
        }

    def _next_claim_id(self, now: datetime) -> str:
        prefix = f"CLM-{now.strftime('%Y%m%d')}-"
        cur = self._execute("SELECT claim_id FROM claims WHERE claim_id LIKE %s", (f"{prefix}%",))
        seqs = [
            int(r["claim_id"].rsplit("-", 1)[-1])
            for r in cur.fetchall()
            if r["claim_id"].rsplit("-", 1)[-1].isdigit()
        ]
        next_seq = (max(seqs) + 1) if seqs else 1
        return f"{prefix}{next_seq:03d}"
