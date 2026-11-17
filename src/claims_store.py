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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from claim_logger import ClaimLogger

STATUS_RECEIVED = "접수"
STATUS_IN_PROGRESS = "처리중"
STATUS_DONE = "완료"

LICENSE_SUBSCRIBED = "구독중"
LICENSE_EXPIRING = "만료임박"
LICENSE_EXPIRED = "만료"
LICENSE_WARNING_DAYS = 30  # 만료 이 날짜 수 이내면 "만료임박"으로 분류

VALID_STATUSES = {STATUS_RECEIVED, STATUS_IN_PROGRESS, STATUS_DONE}
VALID_TRANSITIONS = {
    STATUS_RECEIVED: {STATUS_IN_PROGRESS, STATUS_DONE},
    STATUS_IN_PROGRESS: {STATUS_DONE, STATUS_RECEIVED},
    STATUS_DONE: {STATUS_IN_PROGRESS},  # 재오픈: 완료 후 재발/추가조치 시 처리중으로 되돌릴 수 있다
}

# 업체·사례마다 처리 흐름이 달라 고정 enum으로 강제하지 않는다 — 자유 텍스트이되,
# 프론트엔드 드롭다운에 기본 제공되는 예시 세부 상태.
SUGGESTED_SUB_STATUSES = [
    "접수대기", "담당자배정", "원인분석중", "엔지니어방문예정", "외부업체이관", "고객확인대기", "처리완료", "재오픈",
]

CHANNEL_INTERNAL = "내부입력"
CHANNEL_WEB = "웹"
CHANNEL_APP = "앱"
CHANNEL_SMS = "문자"
CHANNEL_VOICE = "음성"
CHANNEL_IMPORT = "일괄가져오기"

VALID_CHANNELS = {CHANNEL_INTERNAL, CHANNEL_WEB, CHANNEL_APP, CHANNEL_SMS, CHANNEL_VOICE, CHANNEL_IMPORT}

PROCESSING_SELF = "자체처리"
PROCESSING_ENGINEER_VISIT = "엔지니어방문"
PROCESSING_PARTNER = "협력업체이관"
VALID_PROCESSING_TYPES = {"", PROCESSING_SELF, PROCESSING_ENGINEER_VISIT, PROCESSING_PARTNER}

DIFFICULTY_LOW = "low"
DIFFICULTY_MEDIUM = "medium"
DIFFICULTY_HIGH = "high"
VALID_DIFFICULTIES = {"", DIFFICULTY_LOW, DIFFICULTY_MEDIUM, DIFFICULTY_HIGH}

# 예전엔 쉬움/보통/어려움(한글)만 썼다 — 기존 DB에 저장된 값을 새 체계로 한 번만 옮긴다.
_LEGACY_DIFFICULTY_MIGRATION = {"쉬움": DIFFICULTY_LOW, "보통": DIFFICULTY_MEDIUM, "어려움": DIFFICULTY_HIGH}

NOTIFY_TARGET_STAFF = "staff"
NOTIFY_TARGET_CUSTOMER = "customer"
NOTIFY_TARGET_BOTH = "both"
NOTIFY_TARGET_NONE = "none"
VALID_NOTIFY_TARGETS = {NOTIFY_TARGET_STAFF, NOTIFY_TARGET_CUSTOMER, NOTIFY_TARGET_BOTH, NOTIFY_TARGET_NONE}

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
    "processing_type",
    "visit_time",
    "resolution",
    "difficulty",
    "customer_id",
    "product_id",
    "license_id",
    "sub_status",
    "processing_minutes",
    "partner_name",
    "partner_contact",
    "scheduled_visit_at",
    "completed_visit_at",
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
    cost_krw REAL NOT NULL DEFAULT 0,
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
    completed_visit_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_channel ON claims(channel);

CREATE TABLE IF NOT EXISTS claim_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_notes_claim ON claim_customer_notes(claim_id);

CREATE TABLE IF NOT EXISTS manuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_customer ON products(customer_id);

CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

CREATE TABLE IF NOT EXISTS license_notification_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id INTEGER NOT NULL REFERENCES licenses(id),
    sent_at TEXT NOT NULL,
    recipients TEXT NOT NULL DEFAULT '',
    days_left INTEGER,
    success INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_license_notif_license ON license_notification_logs(license_id);
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
        self._migrate_columns()
        self._conn.commit()

        if is_new_db and legacy_csv.exists():
            self._migrate_from_csv(legacy_csv)

    def _migrate_columns(self) -> None:
        """CREATE TABLE IF NOT EXISTS는 이미 있던 테이블에 새 컬럼을 추가해주지 않으므로,
        기존 DB 파일에 이후 버전에서 생긴 컬럼들을 한 번만 보충해준다."""
        claims_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(claims)")}
        for col, ddl in (
            ("processing_type", "TEXT NOT NULL DEFAULT ''"),
            ("visit_time", "TEXT NOT NULL DEFAULT ''"),
            ("resolution", "TEXT NOT NULL DEFAULT ''"),
            ("difficulty", "TEXT NOT NULL DEFAULT ''"),
            ("customer_id", "INTEGER"),
            ("product_id", "INTEGER"),
            ("license_id", "INTEGER"),
            ("sub_status", "TEXT NOT NULL DEFAULT ''"),
            ("processing_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ("partner_name", "TEXT NOT NULL DEFAULT ''"),
            ("partner_contact", "TEXT NOT NULL DEFAULT ''"),
            ("scheduled_visit_at", "TEXT NOT NULL DEFAULT ''"),
            ("completed_visit_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in claims_cols:
                self._conn.execute(f"ALTER TABLE claims ADD COLUMN {col} {ddl}")

        customers_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(customers)")}
        for col in ("email", "memo", "category"):
            if col not in customers_cols:
                self._conn.execute(f"ALTER TABLE customers ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")

        licenses_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(licenses)")}
        for col, ddl in (
            ("warning_days", "INTEGER NOT NULL DEFAULT 30"),
            ("notify_staff_emails", "TEXT NOT NULL DEFAULT ''"),
            ("notify_customer_emails", "TEXT NOT NULL DEFAULT ''"),
            ("notify_target_option", "TEXT NOT NULL DEFAULT 'both'"),
            ("license_key", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in licenses_cols:
                self._conn.execute(f"ALTER TABLE licenses ADD COLUMN {col} {ddl}")

        for old, new in _LEGACY_DIFFICULTY_MIGRATION.items():
            self._conn.execute("UPDATE claims SET difficulty=? WHERE difficulty=?", (new, old))

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
            }
            self._conn.execute(
                f"INSERT INTO claims ({', '.join(FIELDS)}) VALUES ({', '.join('?' for _ in FIELDS)})",
                [row[f] for f in FIELDS],
            )
            self._conn.commit()
            self._record_history(claim_id, changed_by=data.get("assignee", ""), prev_status="", new_status=row["status"], action_type="create", note="")
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
            }
            self._conn.execute(
                f"INSERT INTO claims ({', '.join(FIELDS)}) VALUES ({', '.join('?' for _ in FIELDS)})",
                [row[f] for f in FIELDS],
            )
            self._conn.commit()
            self._record_history(claim_id, changed_by="", prev_status="", new_status=status, action_type="import", note="")

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
        difficulty: Optional[str] = None,
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
        if difficulty:
            clauses.append("difficulty = ?")
            params.append(difficulty)
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
        updates = dict(updates)
        changed_by = updates.pop("changed_by", "") or ""
        history_note = updates.pop("note", "") or ""

        with self._lock:
            current = self._conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
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
                    # 완료 -> 처리중(재오픈): 더 이상 완료 상태가 아니므로 완료일을 지운다.
                    row["completed_at"] = ""

            for key in (
                "assignee", "category", "amount_krw", "description", "tokens_input", "tokens_output", "cost_krw",
                "processing_type", "visit_time", "resolution", "difficulty",
                "customer_id", "product_id", "license_id", "sub_status", "processing_minutes",
                "partner_name", "partner_contact", "scheduled_visit_at", "completed_visit_at",
            ):
                if key in updates and updates[key] is not None:
                    row[key] = updates[key]

            if row.get("processing_type") and row["processing_type"] not in VALID_PROCESSING_TYPES:
                raise ValueError(f"알 수 없는 처리 유형: {row['processing_type']}")
            if row.get("difficulty") and row["difficulty"] not in VALID_DIFFICULTIES:
                raise ValueError(f"알 수 없는 난이도: {row['difficulty']}")

            row["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            self._conn.execute(
                """UPDATE claims SET updated_at=?, status=?, assignee=?, category=?, amount_krw=?,
                   description=?, completed_at=?, tokens_input=?, tokens_output=?, cost_krw=?,
                   processing_type=?, visit_time=?, resolution=?, difficulty=?,
                   customer_id=?, product_id=?, license_id=?, sub_status=?, processing_minutes=?,
                   partner_name=?, partner_contact=?, scheduled_visit_at=?, completed_visit_at=?
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

    def _record_history(
        self, claim_id: str, changed_by: str, prev_status: str, new_status: str, action_type: str, note: str
    ) -> None:
        """claim_history에 변경 이력을 남긴다. 호출자가 이미 self._lock을 쥐고 있다고
        가정한다(재진입 불가능한 threading.Lock이라 여기서 다시 잠그면 교착된다)."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            """INSERT INTO claim_history (claim_id, changed_by, prev_status, new_status, action_type, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (claim_id, changed_by or "", prev_status, new_status, action_type, note or "", now),
        )

    def list_claim_history(self, claim_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM claim_history WHERE claim_id=? ORDER BY created_at, id", (claim_id,)
        ).fetchall()
        return [dict(r) for r in rows]

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

    # ------------------------------------------------------------------ #
    # 계정 (로그인/관리자)
    #
    # password_hash는 절대 이 메서드들 밖으로 반환하지 않는다 — list_accounts에서도
    # 뺀다. 비밀번호 검증 자체는 auth.verify_password가 한다(여기선 저장·조회만).
    ACCOUNT_FIELDS = (
        "username", "password_hash", "role", "display_name", "is_active",
        "created_at", "last_login_at", "failed_attempts", "locked_until",
    )

    def create_account(self, username: str, password_hash: str, role: str, display_name: str = "") -> dict[str, Any]:
        with self._lock:
            if self._conn.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                raise ValueError(f"이미 존재하는 아이디입니다: {username}")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._conn.execute(
                """INSERT INTO accounts (username, password_hash, role, display_name, is_active, created_at)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                (username, password_hash, role, display_name, now),
            )
            self._conn.commit()
            return self._account_public(self.get_account(username))

    def get_account(self, username: str) -> Optional[dict[str, Any]]:
        """비밀번호 해시를 포함한 전체 행. 로그인 검증 전용 — 응답에 그대로 내보내지 말 것."""
        row = self._conn.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
        return [self._account_public(dict(r)) for r in rows]

    def record_login_result(self, username: str, success: bool, max_attempts: int = 5, lock_minutes: int = 15) -> None:
        with self._lock:
            now = datetime.now()
            if success:
                self._conn.execute(
                    "UPDATE accounts SET failed_attempts=0, locked_until='', last_login_at=? WHERE username=?",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), username),
                )
            else:
                row = self._conn.execute("SELECT failed_attempts FROM accounts WHERE username=?", (username,)).fetchone()
                if row is None:
                    self._conn.commit()
                    return
                attempts = row["failed_attempts"] + 1
                locked_until = ""
                if attempts >= max_attempts:
                    locked_until = (now + timedelta(minutes=lock_minutes)).strftime("%Y-%m-%d %H:%M:%S")
                self._conn.execute(
                    "UPDATE accounts SET failed_attempts=?, locked_until=? WHERE username=?",
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
            self._conn.execute("UPDATE accounts SET is_active=? WHERE username=?", (1 if is_active else 0, username))
            self._conn.commit()
            return self._account_public(self.get_account(username))

    def unlock_account(self, username: str) -> dict[str, Any]:
        with self._lock:
            if not self.get_account(username):
                raise ValueError(f"존재하지 않는 아이디입니다: {username}")
            self._conn.execute("UPDATE accounts SET failed_attempts=0, locked_until='' WHERE username=?", (username,))
            self._conn.commit()
            return self._account_public(self.get_account(username))

    def set_account_password(self, username: str, password_hash: str) -> dict[str, Any]:
        with self._lock:
            if not self.get_account(username):
                raise ValueError(f"존재하지 않는 아이디입니다: {username}")
            self._conn.execute(
                "UPDATE accounts SET password_hash=?, failed_attempts=0, locked_until='' WHERE username=?",
                (password_hash, username),
            )
            self._conn.commit()
            return self._account_public(self.get_account(username))

    # ------------------------------------------------------------------ #
    # 고객사 / 제품 / 라이선스
    #
    # 고객사-제품-라이선스 3단 구조. 라이선스 상태(구독중/만료임박/만료)는 저장하지
    # 않고 end_date를 기준으로 조회 시점에 계산한다 — 상태값을 따로 저장하면
    # "오늘 날짜가 지나서 만료됐는데 DB 값은 구독중"처럼 어긋나는 사고가 난다.
    def create_customer(
        self, name: str, contact: str = "", email: str = "", memo: str = "", category: str = ""
    ) -> dict[str, Any]:
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._conn.execute(
                "INSERT INTO customers (name, contact, email, memo, category, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, contact, email, memo, category, now),
            )
            self._conn.commit()
            return self.get_customer(cur.lastrowid)

    def get_customer(self, customer_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        return dict(row) if row else None

    def list_customers(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def update_customer(self, customer_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self.get_customer(customer_id):
                raise ValueError("존재하지 않는 고객사입니다")
            fields = {k: updates[k] for k in ("name", "contact", "email", "memo", "category") if k in updates and updates[k] is not None}
            if fields:
                set_clause = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(f"UPDATE customers SET {set_clause} WHERE id=?", (*fields.values(), customer_id))
                self._conn.commit()
            return self.get_customer(customer_id)

    def delete_customer(self, customer_id: int) -> None:
        with self._lock:
            if self._conn.execute("SELECT 1 FROM products WHERE customer_id=?", (customer_id,)).fetchone():
                raise ValueError("등록된 제품이 있는 고객사는 삭제할 수 없습니다 (먼저 제품을 삭제하세요)")
            self._conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
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
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE customer_id=? ORDER BY created_at DESC", (customer_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def create_product(self, customer_id: int, name: str) -> dict[str, Any]:
        with self._lock:
            if not self.get_customer(customer_id):
                raise ValueError("존재하지 않는 고객사입니다")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._conn.execute(
                "INSERT INTO products (customer_id, name, created_at) VALUES (?, ?, ?)",
                (customer_id, name, now),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def get_product(self, product_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return dict(row) if row else None

    def list_products(self, customer_id: Optional[int] = None) -> list[dict[str, Any]]:
        if customer_id:
            rows = self._conn.execute(
                "SELECT * FROM products WHERE customer_id=? ORDER BY name", (customer_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM products ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def update_product(self, product_id: int, name: str) -> dict[str, Any]:
        with self._lock:
            if not self.get_product(product_id):
                raise ValueError("존재하지 않는 제품입니다")
            self._conn.execute("UPDATE products SET name=? WHERE id=?", (name, product_id))
            self._conn.commit()
            return self.get_product(product_id)

    def delete_product(self, product_id: int) -> None:
        with self._lock:
            if self._conn.execute("SELECT 1 FROM licenses WHERE product_id=?", (product_id,)).fetchone():
                raise ValueError("등록된 라이선스가 있는 제품은 삭제할 수 없습니다 (먼저 라이선스를 삭제하세요)")
            self._conn.execute("DELETE FROM products WHERE id=?", (product_id,))
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
            if not self._conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                raise ValueError("존재하지 않는 제품입니다")
            if notify_target_option not in VALID_NOTIFY_TARGETS:
                raise ValueError(f"알 수 없는 알림 대상 옵션: {notify_target_option}")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._conn.execute(
                """INSERT INTO licenses
                   (product_id, plan_name, start_date, end_date, notify_emails, created_at,
                    warning_days, notify_staff_emails, notify_customer_emails, notify_target_option, license_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    product_id, plan_name, start_date, end_date, notify_emails, now,
                    warning_days, notify_staff_emails, notify_customer_emails, notify_target_option, license_key,
                ),
            )
            self._conn.commit()
            return self._get_license_joined(cur.lastrowid)

    def find_license_by_key(self, license_key: str) -> Optional[dict[str, Any]]:
        """라이선스 키는 고객사·제품과 무관하게 전체에서 유일해야 한다."""
        if not license_key:
            return None
        row = self._conn.execute(self._LICENSE_JOIN_SQL + " WHERE l.license_key=?", (license_key,)).fetchone()
        return self._with_license_status(dict(row)) if row else None

    def update_license(self, license_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM licenses WHERE id=?", (license_id,)).fetchone():
                raise ValueError("존재하지 않는 라이선스입니다")
            allowed = (
                "plan_name", "start_date", "end_date", "notify_emails", "warning_days",
                "notify_staff_emails", "notify_customer_emails", "notify_target_option", "license_key",
            )
            fields = {k: updates[k] for k in allowed if k in updates and updates[k] is not None}
            if "notify_target_option" in fields and fields["notify_target_option"] not in VALID_NOTIFY_TARGETS:
                raise ValueError(f"알 수 없는 알림 대상 옵션: {fields['notify_target_option']}")
            if fields:
                set_clause = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(f"UPDATE licenses SET {set_clause} WHERE id=?", (*fields.values(), license_id))
                self._conn.commit()
            return self._get_license_joined(license_id)

    def delete_license(self, license_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM license_notification_logs WHERE license_id=?", (license_id,))
            self._conn.execute("DELETE FROM licenses WHERE id=?", (license_id,))
            self._conn.commit()

    _LICENSE_JOIN_SQL = """
        SELECT l.*, p.name AS product_name, p.customer_id AS customer_id, c.name AS customer_name
        FROM licenses l
        JOIN products p ON p.id = l.product_id
        JOIN customers c ON c.id = p.customer_id
    """

    def _get_license_joined(self, license_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute(self._LICENSE_JOIN_SQL + " WHERE l.id=?", (license_id,)).fetchone()
        return self._with_license_status(dict(row)) if row else None

    def list_licenses(self, customer_id: Optional[int] = None) -> list[dict[str, Any]]:
        sql = self._LICENSE_JOIN_SQL
        params: list[Any] = []
        if customer_id:
            sql += " WHERE p.customer_id=?"
            params.append(customer_id)
        sql += " ORDER BY l.end_date"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._with_license_status(dict(r)) for r in rows]

    def list_expiring_licenses(self, days: int = LICENSE_WARNING_DAYS) -> list[dict[str, Any]]:
        """만료임박/만료 상태인 라이선스만, 급한 순(만료일 빠른 순)으로."""
        licenses = [
            lic for lic in self.list_licenses() if lic["status"] in (LICENSE_EXPIRING, LICENSE_EXPIRED)
        ]
        licenses.sort(key=lambda lic: lic["end_date"])
        return licenses

    def mark_license_notified(self, license_id: int) -> None:
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._conn.execute("UPDATE licenses SET last_notified_at=? WHERE id=?", (now, license_id))
            self._conn.commit()

    def log_license_notification(
        self, license_id: int, recipients: list[str], days_left: Optional[int], success: bool
    ) -> None:
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._conn.execute(
                """INSERT INTO license_notification_logs (license_id, sent_at, recipients, days_left, success)
                   VALUES (?, ?, ?, ?, ?)""",
                (license_id, now, ", ".join(recipients), days_left, 1 if success else 0),
            )
            self._conn.commit()

    def list_license_notification_logs(self, license_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM license_notification_logs WHERE license_id=? ORDER BY sent_at DESC", (license_id,)
        ).fetchall()
        return [dict(r) for r in rows]

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

    @staticmethod
    def notification_recipients(lic: dict[str, Any]) -> list[str]:
        """notify_target_option에 따라 실제 수신자 목록을 계산한다.

        담당자/고객 이메일을 분리 입력하기 전(과거 데이터)에는 notify_emails 하나만
        채워져 있을 수 있어, 분리 필드가 둘 다 비어 있으면 그걸로 폴백한다."""
        target = lic.get("notify_target_option") or NOTIFY_TARGET_BOTH
        if target == NOTIFY_TARGET_NONE:
            return []
        recipients: list[str] = []
        if target in (NOTIFY_TARGET_STAFF, NOTIFY_TARGET_BOTH):
            recipients += [e.strip() for e in (lic.get("notify_staff_emails") or "").split(",") if e.strip()]
        if target in (NOTIFY_TARGET_CUSTOMER, NOTIFY_TARGET_BOTH):
            recipients += [e.strip() for e in (lic.get("notify_customer_emails") or "").split(",") if e.strip()]
        if not recipients:
            recipients = [e.strip() for e in (lic.get("notify_emails") or "").split(",") if e.strip()]
        return recipients

    # ------------------------------------------------------------------ #
    # 첨부파일 — 실제 파일 바이트는 app.py가 디스크(get_data_root()/data/attachments)에
    # 저장하고, 여기엔 메타데이터(파일명/저장명/용량/업로드 시각)만 기록한다. DB 종류와
    # 무관하게 같은 메타데이터 인터페이스를 쓰도록 Postgres 백엔드에도 동일하게 둔다.
    def add_attachment(
        self, claim_id: str, filename: str, stored_name: str, content_type: str, size_bytes: int
    ) -> dict[str, Any]:
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM claims WHERE claim_id=?", (claim_id,)).fetchone():
                raise ClaimNotFound(claim_id)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._conn.execute(
                """INSERT INTO attachments (claim_id, filename, stored_name, content_type, size_bytes, uploaded_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (claim_id, filename, stored_name, content_type, size_bytes, now),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM attachments WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def list_attachments(self, claim_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM attachments WHERE claim_id=? ORDER BY uploaded_at", (claim_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_attachment(self, attachment_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        return dict(row) if row else None

    def delete_attachment(self, attachment_id: int) -> Optional[dict[str, Any]]:
        """삭제된 행을 돌려준다(호출자가 디스크 파일도 같이 지울 수 있도록) — 없으면 None."""
        with self._lock:
            att = self.get_attachment(attachment_id)
            if att:
                self._conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
                self._conn.commit()
            return att

    # ------------------------------------------------------------------ #
    # 고객 추가 문의/코멘트 — 완료 전 클레임에 대해 고객이 증상을 보충 등록할 수 있다.
    def add_customer_note(self, claim_id: str, body: str) -> dict[str, Any]:
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM claims WHERE claim_id=?", (claim_id,)).fetchone():
                raise ClaimNotFound(claim_id)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._conn.execute(
                "INSERT INTO claim_customer_notes (claim_id, body, created_at) VALUES (?, ?, ?)",
                (claim_id, body, now),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM claim_customer_notes WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def list_customer_notes(self, claim_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM claim_customer_notes WHERE claim_id=? ORDER BY created_at", (claim_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 내부 매뉴얼(FAQ/트러블슈팅) — 유사 클레임과 함께 상세 모달에서 추천에 쓰인다.
    def create_manual(self, title: str, category: str, product_name: str, keywords: str, content: str) -> dict[str, Any]:
        with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._conn.execute(
                """INSERT INTO manuals (title, category, product_name, keywords, content, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (title, category, product_name, keywords, content, now, now),
            )
            self._conn.commit()
            return self.get_manual(cur.lastrowid)

    def get_manual(self, manual_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM manuals WHERE id=?", (manual_id,)).fetchone()
        return dict(row) if row else None

    def list_manuals(self, q: Optional[str] = None) -> list[dict[str, Any]]:
        if q:
            like = f"%{q}%"
            rows = self._conn.execute(
                """SELECT * FROM manuals WHERE title LIKE ? OR keywords LIKE ? OR category LIKE ?
                   OR product_name LIKE ? ORDER BY updated_at DESC""",
                (like, like, like, like),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM manuals ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_manual(self, manual_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self.get_manual(manual_id):
                raise ValueError("존재하지 않는 매뉴얼입니다")
            allowed = ("title", "category", "product_name", "keywords", "content")
            fields = {k: updates[k] for k in allowed if k in updates and updates[k] is not None}
            if fields:
                fields["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                set_clause = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(f"UPDATE manuals SET {set_clause} WHERE id=?", (*fields.values(), manual_id))
                self._conn.commit()
            return self.get_manual(manual_id)

    def delete_manual(self, manual_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM manuals WHERE id=?", (manual_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 유사 클레임 추천 — 같은 제품/유형의 과거 완료 사례 중 처리결과가 기록된 것을 우선
    # 보여준다. 외부 임베딩·벡터 검색 없이 LIKE 기반 키워드 매칭으로 충분히 쓸만하다.
    def find_similar_claims(self, claim_id: str, limit: int = 5) -> list[dict[str, Any]]:
        current = self.get_claim(claim_id)
        clauses = ["status = ?", "claim_id != ?", "resolution != ''"]
        params: list[Any] = [STATUS_DONE, claim_id]
        if current.get("product"):
            clauses.append("product = ?")
            params.append(current["product"])
        if current.get("category"):
            clauses.append("category = ?")
            params.append(current["category"])
        sql = f"SELECT * FROM claims WHERE {' AND '.join(clauses)} ORDER BY completed_at DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, limit)).fetchall()
        results = [dict(r) for r in rows]
        if len(results) < limit and current.get("category"):
            # 제품까지 같은 사례가 부족하면 유형만 같은 사례로 보충한다.
            seen = {r["claim_id"] for r in results}
            sql2 = """SELECT * FROM claims WHERE status=? AND claim_id!=? AND resolution!='' AND category=?
                      ORDER BY completed_at DESC LIMIT ?"""
            for r in self._conn.execute(sql2, (STATUS_DONE, claim_id, current["category"], limit)).fetchall():
                row = dict(r)
                if row["claim_id"] not in seen:
                    results.append(row)
                    seen.add(row["claim_id"])
                if len(results) >= limit:
                    break
        return results[:limit]
