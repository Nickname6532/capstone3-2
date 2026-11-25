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
from datetime import date, datetime, timedelta
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
    LICENSE_EXPIRED,
    LICENSE_EXPIRING,
    LICENSE_SUBSCRIBED,
    LICENSE_WARNING_DAYS,
    NOTIFY_TARGET_BOTH,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_RECEIVED,
    VALID_CHANNELS,
    VALID_DIFFICULTIES,
    VALID_NOTIFY_TARGETS,
    VALID_PROCESSING_TYPES,
    VALID_STATUSES,
    VALID_TRANSITIONS,
    ClaimNotFound,
    ClaimsStore,
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
    cost_krw DOUBLE PRECISION NOT NULL DEFAULT 0,
    processing_type TEXT NOT NULL DEFAULT '',
    visit_time TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL DEFAULT '',
    difficulty TEXT NOT NULL DEFAULT '',
    customer_id INTEGER,
    product_id INTEGER,
    license_id INTEGER,
    sub_status TEXT NOT NULL DEFAULT '',
    processing_minutes INTEGER NOT NULL DEFAULT 0,
    partner_name TEXT NOT NULL DEFAULT '',
    partner_contact TEXT NOT NULL DEFAULT '',
    scheduled_visit_at TEXT NOT NULL DEFAULT '',
    completed_visit_at TEXT NOT NULL DEFAULT '',
    issue_started_at TEXT NOT NULL DEFAULT '',
    issue_ended_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_channel ON claims(channel);

ALTER TABLE claims ADD COLUMN IF NOT EXISTS processing_type TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS visit_time TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS resolution TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS difficulty TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS customer_id INTEGER;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS product_id INTEGER;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS license_id INTEGER;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS sub_status TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS processing_minutes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS partner_name TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS partner_contact TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS scheduled_visit_at TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS completed_visit_at TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS issue_started_at TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN IF NOT EXISTS issue_ended_at TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS claim_history (
    id SERIAL PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    changed_by TEXT NOT NULL DEFAULT '',
    prev_status TEXT NOT NULL DEFAULT '',
    new_status TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL DEFAULT 'update',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_history_claim ON claim_history(claim_id);

CREATE TABLE IF NOT EXISTS claim_customer_notes (
    id SERIAL PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_notes_claim ON claim_customer_notes(claim_id);

CREATE TABLE IF NOT EXISTS manuals (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manuals_category ON manuals(category);

CREATE TABLE IF NOT EXISTS attachments (
    id SERIAL PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_claim ON attachments(claim_id);

CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login_at TEXT NOT NULL DEFAULT '',
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS customers (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT '';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS memo TEXT NOT NULL DEFAULT '';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_customer ON products(customer_id);

CREATE TABLE IF NOT EXISTS licenses (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    plan_name TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    notify_emails TEXT NOT NULL DEFAULT '',
    last_notified_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    warning_days INTEGER NOT NULL DEFAULT 30,
    notify_staff_emails TEXT NOT NULL DEFAULT '',
    notify_customer_emails TEXT NOT NULL DEFAULT '',
    notify_target_option TEXT NOT NULL DEFAULT 'both',
    license_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_licenses_product ON licenses(product_id);
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS warning_days INTEGER NOT NULL DEFAULT 30;
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS notify_staff_emails TEXT NOT NULL DEFAULT '';
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS notify_customer_emails TEXT NOT NULL DEFAULT '';
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS notify_target_option TEXT NOT NULL DEFAULT 'both';
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS license_key TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS license_notification_logs (
    id SERIAL PRIMARY KEY,
    license_id INTEGER NOT NULL REFERENCES licenses(id),
    sent_at TEXT NOT NULL,
    recipients TEXT NOT NULL DEFAULT '',
    days_left INTEGER,
    success INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_license_notif_license ON license_notification_logs(license_id);
"""


class PgClaimsStore:
    def __init__(self, database_url: str):
        self.database_url = _sanitize_conninfo(database_url)
        self._lock = threading.Lock()
        self._conn: Optional[psycopg.Connection] = None
        self._ensure_conn()
        # 서버리스는 콜드스타트가 동시에 여러 개 일어난다. 각자 ALTER TABLE을 실행하면
        # 서로 락을 기다리며 deadlock이 나므로, advisory lock으로 한 번에 하나만 초기화한다.
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(7351001)")
            try:
                cur.execute(_SCHEMA)
                # 예전엔 난이도를 쉬움/보통/어려움(한글)으로 저장했다 — low/medium/high로 한 번만 옮긴다.
                for old, new in (("쉬움", "low"), ("보통", "medium"), ("어려움", "high")):
                    cur.execute("UPDATE claims SET difficulty=%s WHERE difficulty=%s", (new, old))
            finally:
                cur.execute("SELECT pg_advisory_unlock(7351001)")

    # ------------------------------------------------------------------ #
    def _ensure_conn(self) -> psycopg.Connection:
        """연결이 끊겼으면(서버리스 웜 인스턴스 재사용 중 idle timeout 등) 새로 연다."""
        if self._conn is None or self._conn.closed:
            # autocommit=True: 읽기만 해도 트랜잭션이 열린 채 남으면 DB의
            # idle_in_transaction_session_timeout이 연결을 끊어 이후 요청이 전부 500이 된다.
            # prepare_threshold=None: Supabase/PgBouncer의 커넥션 풀러(트랜잭션 모드)는
            # 서버사이드 prepared statement를 커넥션 간에 유지하지 않아서, 기본값대로 두면
            # "prepared statement already exists" 류 오류가 날 수 있어 아예 끈다.
            self._conn = psycopg.connect(
                self.database_url, row_factory=dict_row, autocommit=True, prepare_threshold=None
            )
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> psycopg.Cursor:
        conn = self._ensure_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur
        except (psycopg.OperationalError, psycopg.errors.InvalidTransactionState):
            self._conn = None
            conn = self._ensure_conn()
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur
        except psycopg.Error:
            # 실패한 트랜잭션을 그대로 두면 이 연결의 이후 모든 쿼리가
            # "current transaction is aborted"로 실패한다(웜 인스턴스가 연결을 재사용).
            try:
                conn.rollback()
            except psycopg.Error:
                self._conn = None
            raise

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
                "processing_type": data.get("processing_type", ""),
                "visit_time": data.get("visit_time", ""),
                "resolution": data.get("resolution", ""),
                "difficulty": data.get("difficulty", ""),
                "customer_id": data.get("customer_id"),
                "product_id": data.get("product_id"),
                "license_id": data.get("license_id"),
                "sub_status": data.get("sub_status", ""),
                "processing_minutes": int(data.get("processing_minutes") or 0),
                "partner_name": data.get("partner_name", ""),
                "partner_contact": data.get("partner_contact", ""),
                "scheduled_visit_at": data.get("scheduled_visit_at", ""),
                "completed_visit_at": data.get("completed_visit_at", ""),
                "issue_started_at": data.get("issue_started_at", ""),
                "issue_ended_at": data.get("issue_ended_at", ""),
            }
            placeholders = ", ".join(f"%({f})s" for f in FIELDS)
            self._execute(f"INSERT INTO claims ({', '.join(FIELDS)}) VALUES ({placeholders})", row)
            self._record_history(claim_id, changed_by=data.get("assignee", ""), prev_status="", new_status=row["status"], action_type="create", note="")
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
                "processing_type": data.get("processing_type", ""),
                "visit_time": data.get("visit_time", ""),
                "resolution": data.get("resolution", ""),
                "difficulty": data.get("difficulty", ""),
                "customer_id": data.get("customer_id"),
                "product_id": data.get("product_id"),
                "license_id": data.get("license_id"),
                "sub_status": data.get("sub_status", ""),
                "processing_minutes": int(data.get("processing_minutes") or 0),
                "partner_name": data.get("partner_name", ""),
                "partner_contact": data.get("partner_contact", ""),
                "scheduled_visit_at": data.get("scheduled_visit_at", ""),
                "completed_visit_at": data.get("completed_visit_at", ""),
                "issue_started_at": data.get("issue_started_at", ""),
                "issue_ended_at": data.get("issue_ended_at", ""),
            }
            placeholders = ", ".join(f"%({f})s" for f in FIELDS)
            self._execute(f"INSERT INTO claims ({', '.join(FIELDS)}) VALUES ({placeholders})", row)
            self._record_history(claim_id, changed_by="", prev_status="", new_status=status, action_type="import", note="")
            self._conn.commit()
            return row

    def list_claims(
        self,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        channel: Optional[str] = None,
        contact: Optional[str] = None,
        q: Optional[str] = None,
        difficulty: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
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
        if difficulty:
            clauses.append("difficulty = %s")
            params.append(difficulty)
        if contact:
            clauses.append("REPLACE(contact, '-', '') LIKE %s")
            digits_only = "".join(ch for ch in contact if ch.isdigit())
            params.append(f"%{digits_only}%")
        # 기간 검색: 장애 발생시점(없으면 접수일) 날짜 기준, 양 끝 포함.
        if date_from:
            clauses.append("substr(COALESCE(NULLIF(issue_started_at, ''), created_at), 1, 10) >= %s")
            params.append(date_from[:10])
        if date_to:
            clauses.append("substr(COALESCE(NULLIF(issue_started_at, ''), created_at), 1, 10) <= %s")
            params.append(date_to[:10])
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
        updates = dict(updates)
        changed_by = updates.pop("changed_by", "") or ""
        history_note = updates.pop("note", "") or ""

        with self._lock:
            cur = self._execute("SELECT * FROM claims WHERE claim_id = %s", (claim_id,))
            current = cur.fetchone()
            if current is None:
                raise ClaimNotFound(claim_id)
            row = dict(current)
            prev_status = row["status"]

            new_status = updates.get("status")
            if new_status is not None and new_status != row["status"]:
                if new_status not in VALID_STATUSES:
                    raise InvalidTransition(f"알 수 없는 상태: {new_status}")
                if new_status not in VALID_TRANSITIONS[row["status"]]:
                    raise InvalidTransition(f"{row['status']} -> {new_status} 전환 불가")
                row["status"] = new_status
                if new_status == STATUS_DONE:
                    row["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                elif prev_status == STATUS_DONE:
                    row["completed_at"] = ""

            for key in (
                "assignee", "category", "amount_krw", "description", "tokens_input", "tokens_output", "cost_krw",
                "processing_type", "visit_time", "resolution", "difficulty",
                "customer_id", "product_id", "license_id", "sub_status", "processing_minutes",
                "partner_name", "partner_contact", "scheduled_visit_at", "completed_visit_at",
                "issue_started_at", "issue_ended_at",
            ):
                if key in updates and updates[key] is not None:
                    row[key] = updates[key]

            if row.get("processing_type") and row["processing_type"] not in VALID_PROCESSING_TYPES:
                raise ValueError(f"알 수 없는 처리 유형: {row['processing_type']}")
            if row.get("difficulty") and row["difficulty"] not in VALID_DIFFICULTIES:
                raise ValueError(f"알 수 없는 난이도: {row['difficulty']}")

            row["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            self._execute(
                """UPDATE claims SET updated_at=%s, status=%s, assignee=%s, category=%s, amount_krw=%s,
                   description=%s, completed_at=%s, tokens_input=%s, tokens_output=%s, cost_krw=%s,
                   processing_type=%s, visit_time=%s, resolution=%s, difficulty=%s,
                   customer_id=%s, product_id=%s, license_id=%s, sub_status=%s, processing_minutes=%s,
                   partner_name=%s, partner_contact=%s, scheduled_visit_at=%s, completed_visit_at=%s,
                   issue_started_at=%s, issue_ended_at=%s
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
                    row["processing_type"],
                    row["visit_time"],
                    row["resolution"],
                    row["difficulty"],
                    row.get("customer_id"),
                    row.get("product_id"),
                    row.get("license_id"),
                    row.get("sub_status", ""),
                    row.get("processing_minutes", 0),
                    row.get("partner_name", ""),
                    row.get("partner_contact", ""),
                    row.get("scheduled_visit_at", ""),
                    row.get("completed_visit_at", ""),
                    row.get("issue_started_at", ""),
                    row.get("issue_ended_at", ""),
                    claim_id,
                ),
            )

            if row["status"] != prev_status or history_note:
                self._record_history(
                    claim_id, changed_by=changed_by, prev_status=prev_status, new_status=row["status"],
                    action_type="reopen" if (prev_status == STATUS_DONE and row["status"] != STATUS_DONE) else "status_change",
                    note=history_note,
                )
            self._conn.commit()
            return row

    def _record_history(
        self, claim_id: str, changed_by: str, prev_status: str, new_status: str, action_type: str, note: str
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._execute(
            """INSERT INTO claim_history (claim_id, changed_by, prev_status, new_status, action_type, note, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (claim_id, changed_by or "", prev_status, new_status, action_type, note or "", now),
        )

    def list_claim_history(self, claim_id: str) -> list[dict[str, Any]]:
        cur = self._execute("SELECT * FROM claim_history WHERE claim_id=%s ORDER BY created_at, id", (claim_id,))
        return [dict(r) for r in cur.fetchall()]

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

    # ------------------------------------------------------------------ #
    # 계정 (로그인/관리자) — SQLite 버전(ClaimsStore)과 동일한 인터페이스
    def create_account(self, username: str, password_hash: str, role: str, display_name: str = "") -> dict[str, Any]:
        with self._lock:
            cur = self._execute("SELECT 1 FROM accounts WHERE username=%s", (username,))
            if cur.fetchone():
                raise ValueError(f"이미 존재하는 아이디입니다: {username}")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._execute(
                """INSERT INTO accounts (username, password_hash, role, display_name, is_active, created_at)
                   VALUES (%s, %s, %s, %s, 1, %s)""",
                (username, password_hash, role, display_name, now),
            )
            self._conn.commit()
            return self._account_public(self.get_account(username))

    def get_account(self, username: str) -> Optional[dict[str, Any]]:
        cur = self._execute("SELECT * FROM accounts WHERE username=%s", (username,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        cur = self._execute("SELECT * FROM accounts ORDER BY created_at")
        return [self._account_public(dict(r)) for r in cur.fetchall()]

    def record_login_result(self, username: str, success: bool, max_attempts: int = 5, lock_minutes: int = 15) -> None:
        with self._lock:
            now = datetime.now()
            if success:
                self._execute(
                    "UPDATE accounts SET failed_attempts=0, locked_until='', last_login_at=%s WHERE username=%s",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), username),
                )
            else:
                cur = self._execute("SELECT failed_attempts FROM accounts WHERE username=%s", (username,))
                row = cur.fetchone()
                if row is None:
                    self._conn.commit()
                    return
                attempts = row["failed_attempts"] + 1
                locked_until = ""
                if attempts >= max_attempts:
                    locked_until = (now + timedelta(minutes=lock_minutes)).strftime("%Y-%m-%d %H:%M:%S")
                self._execute(
                    "UPDATE accounts SET failed_attempts=%s, locked_until=%s WHERE username=%s",
                    (attempts, locked_until, username),
                )
            self._conn.commit()

    @staticmethod
    def _account_public(account: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in account.items() if k != "password_hash"}

    def set_account_active(self, username: str, is_active: bool) -> dict[str, Any]:
        with self._lock:
            if not self.get_account(username):
                raise ValueError(f"존재하지 않는 아이디입니다: {username}")
            self._execute("UPDATE accounts SET is_active=%s WHERE username=%s", (1 if is_active else 0, username))
            self._conn.commit()
            return self._account_public(self.get_account(username))

    def unlock_account(self, username: str) -> dict[str, Any]:
        with self._lock:
            if not self.get_account(username):
                raise ValueError(f"존재하지 않는 아이디입니다: {username}")
            self._execute("UPDATE accounts SET failed_attempts=0, locked_until='' WHERE username=%s", (username,))
            self._conn.commit()
            return self._account_public(self.get_account(username))

    def set_account_password(self, username: str, password_hash: str) -> dict[str, Any]:
        with self._lock:
            if not self.get_account(username):
                raise ValueError(f"존재하지 않는 아이디입니다: {username}")
            self._execute(
                "UPDATE accounts SET password_hash=%s, failed_attempts=0, locked_until='' WHERE username=%s",
                (password_hash, username),
            )
            self._conn.commit()
            return self._account_public(self.get_account(username))

    # ------------------------------------------------------------------ #
    # 고객사 / 제품 / 라이선스 — SQLite 버전(ClaimsStore)과 동일한 인터페이스
    def create_customer(
        self, name: str, contact: str = "", email: str = "", memo: str = "", category: str = ""
    ) -> dict[str, Any]:
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._execute(
                "INSERT INTO customers (name, contact, email, memo, category, created_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (name, contact, email, memo, category, now),
            )
            new_id = cur.fetchone()["id"]
            self._conn.commit()
            return self.get_customer(new_id)

    def get_customer(self, customer_id: int) -> Optional[dict[str, Any]]:
        cur = self._execute("SELECT * FROM customers WHERE id=%s", (customer_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_customers(self) -> list[dict[str, Any]]:
        cur = self._execute("SELECT * FROM customers ORDER BY name")
        return [dict(r) for r in cur.fetchall()]

    def update_customer(self, customer_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self.get_customer(customer_id):
                raise ValueError("존재하지 않는 고객사입니다")
            fields = {k: updates[k] for k in ("name", "contact", "email", "memo", "category") if k in updates and updates[k] is not None}
            if fields:
                set_clause = ", ".join(f"{k}=%s" for k in fields)
                self._execute(f"UPDATE customers SET {set_clause} WHERE id=%s", (*fields.values(), customer_id))
                self._conn.commit()
            return self.get_customer(customer_id)

    def delete_customer(self, customer_id: int) -> None:
        with self._lock:
            cur = self._execute("SELECT 1 FROM products WHERE customer_id=%s", (customer_id,))
            if cur.fetchone():
                raise ValueError("등록된 제품이 있는 고객사는 삭제할 수 없습니다 (먼저 제품을 삭제하세요)")
            self._execute("DELETE FROM customers WHERE id=%s", (customer_id,))
            self._conn.commit()

    def get_customer_overview(self, customer_id: int) -> dict[str, Any]:
        customer = self.get_customer(customer_id)
        if not customer:
            raise ValueError("존재하지 않는 고객사입니다")
        products = self.list_products(customer_id=customer_id)
        licenses = self.list_licenses(customer_id=customer_id)
        claims = self.list_claims_by_customer(customer_id)
        return {
            "customer": customer,
            "products": products,
            "licenses": licenses,
            "claims": claims,
            "claim_count": len(claims),
        }

    def list_claims_by_customer(self, customer_id: int) -> list[dict[str, Any]]:
        cur = self._execute("SELECT * FROM claims WHERE customer_id=%s ORDER BY created_at DESC", (customer_id,))
        return [dict(r) for r in cur.fetchall()]

    def create_product(self, customer_id: int, name: str) -> dict[str, Any]:
        with self._lock:
            if not self.get_customer(customer_id):
                raise ValueError("존재하지 않는 고객사입니다")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._execute(
                "INSERT INTO products (customer_id, name, created_at) VALUES (%s, %s, %s) RETURNING id",
                (customer_id, name, now),
            )
            new_id = cur.fetchone()["id"]
            self._conn.commit()
            cur = self._execute("SELECT * FROM products WHERE id=%s", (new_id,))
            return dict(cur.fetchone())

    def get_product(self, product_id: int) -> Optional[dict[str, Any]]:
        cur = self._execute("SELECT * FROM products WHERE id=%s", (product_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_products(self, customer_id: Optional[int] = None) -> list[dict[str, Any]]:
        if customer_id:
            cur = self._execute("SELECT * FROM products WHERE customer_id=%s ORDER BY name", (customer_id,))
        else:
            cur = self._execute("SELECT * FROM products ORDER BY name")
        return [dict(r) for r in cur.fetchall()]

    def update_product(self, product_id: int, name: str) -> dict[str, Any]:
        with self._lock:
            if not self.get_product(product_id):
                raise ValueError("존재하지 않는 제품입니다")
            self._execute("UPDATE products SET name=%s WHERE id=%s", (name, product_id))
            self._conn.commit()
            return self.get_product(product_id)

    def delete_product(self, product_id: int) -> None:
        with self._lock:
            cur = self._execute("SELECT 1 FROM licenses WHERE product_id=%s", (product_id,))
            if cur.fetchone():
                raise ValueError("등록된 라이선스가 있는 제품은 삭제할 수 없습니다 (먼저 라이선스를 삭제하세요)")
            self._execute("DELETE FROM products WHERE id=%s", (product_id,))
            self._conn.commit()

    def create_license(
        self,
        product_id: int,
        plan_name: str,
        start_date: str,
        end_date: str,
        notify_emails: str = "",
        warning_days: int = LICENSE_WARNING_DAYS,
        notify_staff_emails: str = "",
        notify_customer_emails: str = "",
        notify_target_option: str = NOTIFY_TARGET_BOTH,
        license_key: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            cur = self._execute("SELECT 1 FROM products WHERE id=%s", (product_id,))
            if not cur.fetchone():
                raise ValueError("존재하지 않는 제품입니다")
            if notify_target_option not in VALID_NOTIFY_TARGETS:
                raise ValueError(f"알 수 없는 알림 대상 옵션: {notify_target_option}")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._execute(
                """INSERT INTO licenses
                   (product_id, plan_name, start_date, end_date, notify_emails, created_at,
                    warning_days, notify_staff_emails, notify_customer_emails, notify_target_option, license_key)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (
                    product_id, plan_name, start_date, end_date, notify_emails, now,
                    warning_days, notify_staff_emails, notify_customer_emails, notify_target_option, license_key,
                ),
            )
            new_id = cur.fetchone()["id"]
            self._conn.commit()
            return self._get_license_joined(new_id)

    def find_license_by_key(self, license_key: str) -> Optional[dict[str, Any]]:
        """라이선스 키는 고객사·제품과 무관하게 전체에서 유일해야 한다."""
        if not license_key:
            return None
        cur = self._execute(self._LICENSE_JOIN_SQL + " WHERE l.license_key=%s", (license_key,))
        row = cur.fetchone()
        return self._with_license_status(dict(row)) if row else None

    def update_license(self, license_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cur = self._execute("SELECT 1 FROM licenses WHERE id=%s", (license_id,))
            if not cur.fetchone():
                raise ValueError("존재하지 않는 라이선스입니다")
            allowed = (
                "plan_name", "start_date", "end_date", "notify_emails", "warning_days",
                "notify_staff_emails", "notify_customer_emails", "notify_target_option", "license_key",
            )
            fields = {k: updates[k] for k in allowed if k in updates and updates[k] is not None}
            if "notify_target_option" in fields and fields["notify_target_option"] not in VALID_NOTIFY_TARGETS:
                raise ValueError(f"알 수 없는 알림 대상 옵션: {fields['notify_target_option']}")
            if fields:
                set_clause = ", ".join(f"{k}=%s" for k in fields)
                self._execute(f"UPDATE licenses SET {set_clause} WHERE id=%s", (*fields.values(), license_id))
                self._conn.commit()
            return self._get_license_joined(license_id)

    def delete_license(self, license_id: int) -> None:
        with self._lock:
            self._execute("DELETE FROM license_notification_logs WHERE license_id=%s", (license_id,))
            self._execute("DELETE FROM licenses WHERE id=%s", (license_id,))
            self._conn.commit()

    _LICENSE_JOIN_SQL = """
        SELECT l.*, p.name AS product_name, p.customer_id AS customer_id, c.name AS customer_name
        FROM licenses l
        JOIN products p ON p.id = l.product_id
        JOIN customers c ON c.id = p.customer_id
    """

    def _get_license_joined(self, license_id: int) -> Optional[dict[str, Any]]:
        cur = self._execute(self._LICENSE_JOIN_SQL + " WHERE l.id=%s", (license_id,))
        row = cur.fetchone()
        return self._with_license_status(dict(row)) if row else None

    def list_licenses(self, customer_id: Optional[int] = None) -> list[dict[str, Any]]:
        sql = self._LICENSE_JOIN_SQL
        params: list[Any] = []
        if customer_id:
            sql += " WHERE p.customer_id=%s"
            params.append(customer_id)
        sql += " ORDER BY l.end_date"
        cur = self._execute(sql, tuple(params))
        return [self._with_license_status(dict(r)) for r in cur.fetchall()]

    def list_expiring_licenses(self, days: int = LICENSE_WARNING_DAYS) -> list[dict[str, Any]]:
        licenses = [
            lic for lic in self.list_licenses() if lic["status"] in (LICENSE_EXPIRING, LICENSE_EXPIRED)
        ]
        licenses.sort(key=lambda lic: lic["end_date"])
        return licenses

    def mark_license_notified(self, license_id: int) -> None:
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._execute("UPDATE licenses SET last_notified_at=%s WHERE id=%s", (now, license_id))
            self._conn.commit()

    def log_license_notification(
        self, license_id: int, recipients: list[str], days_left: Optional[int], success: bool
    ) -> None:
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._execute(
                """INSERT INTO license_notification_logs (license_id, sent_at, recipients, days_left, success)
                   VALUES (%s, %s, %s, %s, %s)""",
                (license_id, now, ", ".join(recipients), days_left, 1 if success else 0),
            )
            self._conn.commit()

    def list_license_notification_logs(self, license_id: int) -> list[dict[str, Any]]:
        cur = self._execute(
            "SELECT * FROM license_notification_logs WHERE license_id=%s ORDER BY sent_at DESC", (license_id,)
        )
        return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def _with_license_status(row: dict[str, Any]) -> dict[str, Any]:
        try:
            end = date.fromisoformat(str(row["end_date"])[:10])
            days_left = (end - date.today()).days
        except (ValueError, TypeError):
            days_left = None

        warning_days = row.get("warning_days") or LICENSE_WARNING_DAYS

        if days_left is None:
            status = "알수없음"
        elif days_left < 0:
            status = LICENSE_EXPIRED
        elif days_left <= warning_days:
            status = LICENSE_EXPIRING
        else:
            status = LICENSE_SUBSCRIBED

        row["status"] = status
        row["days_left"] = days_left
        return row

    # notification_recipients는 DB에 의존하지 않는 순수 계산이라 SQLite 버전(ClaimsStore)의
    # 구현을 그대로 재사용한다 — 백엔드마다 따로 유지하면 로직이 갈라질 위험이 있다.
    notification_recipients = staticmethod(ClaimsStore.notification_recipients)

    # ------------------------------------------------------------------ #
    # 첨부파일 — SQLite 버전(ClaimsStore)과 동일한 메타데이터 인터페이스. 파일 바이트
    # 자체는 app.py가 디스크에 저장하고, 여기엔 메타데이터만 기록한다.
    def add_attachment(
        self, claim_id: str, filename: str, stored_name: str, content_type: str, size_bytes: int
    ) -> dict[str, Any]:
        with self._lock:
            cur = self._execute("SELECT 1 FROM claims WHERE claim_id=%s", (claim_id,))
            if not cur.fetchone():
                raise ClaimNotFound(claim_id)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._execute(
                """INSERT INTO attachments (claim_id, filename, stored_name, content_type, size_bytes, uploaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (claim_id, filename, stored_name, content_type, size_bytes, now),
            )
            new_id = cur.fetchone()["id"]
            self._conn.commit()
            cur = self._execute("SELECT * FROM attachments WHERE id=%s", (new_id,))
            return dict(cur.fetchone())

    def list_attachments(self, claim_id: str) -> list[dict[str, Any]]:
        cur = self._execute("SELECT * FROM attachments WHERE claim_id=%s ORDER BY uploaded_at", (claim_id,))
        return [dict(r) for r in cur.fetchall()]

    def get_attachment(self, attachment_id: int) -> Optional[dict[str, Any]]:
        cur = self._execute("SELECT * FROM attachments WHERE id=%s", (attachment_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def delete_attachment(self, attachment_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            att = self.get_attachment(attachment_id)
            if att:
                self._execute("DELETE FROM attachments WHERE id=%s", (attachment_id,))
                self._conn.commit()
            return att

    # ------------------------------------------------------------------ #
    # 고객 추가 문의/코멘트
    def add_customer_note(self, claim_id: str, body: str) -> dict[str, Any]:
        with self._lock:
            cur = self._execute("SELECT 1 FROM claims WHERE claim_id=%s", (claim_id,))
            if not cur.fetchone():
                raise ClaimNotFound(claim_id)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._execute(
                "INSERT INTO claim_customer_notes (claim_id, body, created_at) VALUES (%s, %s, %s) RETURNING id",
                (claim_id, body, now),
            )
            new_id = cur.fetchone()["id"]
            self._conn.commit()
            cur = self._execute("SELECT * FROM claim_customer_notes WHERE id=%s", (new_id,))
            return dict(cur.fetchone())

    def list_customer_notes(self, claim_id: str) -> list[dict[str, Any]]:
        cur = self._execute("SELECT * FROM claim_customer_notes WHERE claim_id=%s ORDER BY created_at", (claim_id,))
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # 내부 매뉴얼(FAQ/트러블슈팅)
    def create_manual(self, title: str, category: str, product_name: str, keywords: str, content: str) -> dict[str, Any]:
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._execute(
                """INSERT INTO manuals (title, category, product_name, keywords, content, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (title, category, product_name, keywords, content, now, now),
            )
            new_id = cur.fetchone()["id"]
            self._conn.commit()
            return self.get_manual(new_id)

    def get_manual(self, manual_id: int) -> Optional[dict[str, Any]]:
        cur = self._execute("SELECT * FROM manuals WHERE id=%s", (manual_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_manuals(self, q: Optional[str] = None) -> list[dict[str, Any]]:
        if q:
            like = f"%{q}%"
            cur = self._execute(
                """SELECT * FROM manuals WHERE title ILIKE %s OR keywords ILIKE %s OR category ILIKE %s
                   OR product_name ILIKE %s ORDER BY updated_at DESC""",
                (like, like, like, like),
            )
        else:
            cur = self._execute("SELECT * FROM manuals ORDER BY updated_at DESC")
        return [dict(r) for r in cur.fetchall()]

    def update_manual(self, manual_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self.get_manual(manual_id):
                raise ValueError("존재하지 않는 매뉴얼입니다")
            allowed = ("title", "category", "product_name", "keywords", "content")
            fields = {k: updates[k] for k in allowed if k in updates and updates[k] is not None}
            if fields:
                fields["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                set_clause = ", ".join(f"{k}=%s" for k in fields)
                self._execute(f"UPDATE manuals SET {set_clause} WHERE id=%s", (*fields.values(), manual_id))
                self._conn.commit()
            return self.get_manual(manual_id)

    def delete_manual(self, manual_id: int) -> None:
        with self._lock:
            self._execute("DELETE FROM manuals WHERE id=%s", (manual_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 유사 클레임 추천
    def find_similar_claims(self, claim_id: str, limit: int = 5) -> list[dict[str, Any]]:
        current = self.get_claim(claim_id)
        clauses = ["status = %s", "claim_id != %s", "resolution != ''"]
        params: list[Any] = [STATUS_DONE, claim_id]
        if current.get("product"):
            clauses.append("product = %s")
            params.append(current["product"])
        if current.get("category"):
            clauses.append("category = %s")
            params.append(current["category"])
        sql = f"SELECT * FROM claims WHERE {' AND '.join(clauses)} ORDER BY completed_at DESC LIMIT %s"
        cur = self._execute(sql, (*params, limit))
        results = [dict(r) for r in cur.fetchall()]
        if len(results) < limit and current.get("category"):
            seen = {r["claim_id"] for r in results}
            sql2 = """SELECT * FROM claims WHERE status=%s AND claim_id!=%s AND resolution!='' AND category=%s
                      ORDER BY completed_at DESC LIMIT %s"""
            cur2 = self._execute(sql2, (STATUS_DONE, claim_id, current["category"], limit))
            for r in cur2.fetchall():
                row = dict(r)
                if row["claim_id"] not in seen:
                    results.append(row)
                    seen.add(row["claim_id"])
                if len(results) >= limit:
                    break
        return results[:limit]
