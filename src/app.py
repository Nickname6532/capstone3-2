"""
고객 클레임 접수/처리 통합관리 시스템 - FastAPI 백엔드.

실행:
    .venv/bin/uvicorn app:app --reload --port 8000
    (src/ 디렉터리에서 실행)

로그인: 최초 관리자 계정은 create_admin.py로 터미널에서 직접 만든다(코드에
기본 비밀번호를 남기지 않기 위함). README의 "로그인/계정" 절 참고.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import os
import secrets
import sqlite3
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import bulk_import
import email_notify
import license_import
import storage as storage_module
from auth import (
    ROLE_ADMIN,
    ROLE_STAFF,
    hash_password,
    validate_password_strength,
    validate_username,
    verify_password,
)
from claims_store import (
    CHANNEL_APP,
    CHANNEL_INTERNAL,
    CHANNEL_SMS,
    CHANNEL_WEB,
    STATUS_DONE,
    ClaimNotFound,
    DuplicateClaim,
    InvalidTransition,
)
from db import build_store, get_data_root, get_database_url

app = FastAPI(title="클레임 통합관리 시스템")

BASE_DIR = Path(__file__).resolve().parent
IS_VERCEL = bool(os.environ.get("VERCEL"))

store, logger = build_store()

# Vercel Cron(GET 요청으로 트리거)이 /api/licenses/check-expiry를 호출할 때 쓰는 공유 비밀값.
# Vercel이 cron job에는 자동으로 "Authorization: Bearer <CRON_SECRET>"을 붙여서 호출해준다.
CRON_SECRET = os.environ.get("CRON_SECRET")

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 클레임 첨부파일(이미지/영상) 저장 위치 — DB(SQLite/Postgres)엔 메타데이터만 두고
# 실제 바이트는 디스크에 둔다. Vercel 등 서버리스에서는 claims.db와 같은 이유로
# /tmp에 쓰이므로 인스턴스가 바뀌면 사라질 수 있다(README에 안내).
ATTACHMENTS_DIR = get_data_root() / "data" / "attachments"
# S3_BUCKET 환경변수가 있으면 S3 호환 스토리지로, 없으면 로컬 디스크로 자동 전환된다
# (storage.py). Vercel 등 서버리스는 로컬 디스크가 휘발되니 운영 단계엔 S3 연동 권장.
attachment_storage = storage_module.build_attachment_storage(ATTACHMENTS_DIR)

ALLOWED_ATTACHMENT_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "video/mp4", "video/webm", "video/quicktime",
}
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25MB

# 세션 쿠키 서명 키. 반드시 환경변수로 고정값을 줘야 한다 — 특히 Vercel 서버리스는
# 인스턴스마다 프로세스가 새로 뜨는데, 그때마다 키가 달라지면 한 인스턴스에서 로그인한
# 세션을 다른 인스턴스가 검증하지 못해 로그인이 수시로 풀리는 문제가 생긴다.
SESSION_SECRET = os.environ.get("SESSION_SECRET")
if not SESSION_SECRET:
    if IS_VERCEL:
        print(
            "경고: SESSION_SECRET 환경변수가 없습니다. 인스턴스마다 키가 달라져 로그인이 "
            "수시로 풀립니다. Vercel 프로젝트 환경변수에 SESSION_SECRET을 추가하세요.",
            file=sys.stderr,
        )
    SESSION_SECRET = secrets.token_hex(32)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="claim_session",
    same_site="strict",
    https_only=IS_VERCEL or bool(get_database_url()),
    max_age=60 * 60 * 12,  # 12시간
)


# 사내 업무 시스템용 IP 화이트리스트. ALLOWED_IPS(쉼표 구분 IP/CIDR, 예:
# "127.0.0.1,192.168.0.0/16")가 설정된 경우에만 활성화된다 — 비어있으면 기존처럼
# 모든 IP를 허용해 로컬 개발/외부 데모가 그대로 된다.
_ALLOWED_NETWORKS: list = []
for _part in os.environ.get("ALLOWED_IPS", "").split(","):
    _part = _part.strip()
    if not _part:
        continue
    try:
        _ALLOWED_NETWORKS.append(ipaddress.ip_network(_part, strict=False))
    except ValueError:
        print(f"경고: ALLOWED_IPS의 잘못된 항목을 무시합니다: {_part}", file=sys.stderr)

# 고객용 공개 경로와 Vercel Cron은 IP 제한 대상에서 제외한다.
_IP_EXEMPT_PREFIXES = ("/submit", "/track", "/static/", "/api/public/")
_IP_EXEMPT_EXACT = {"/api/channels/sms"}


def _client_ip(request: Request) -> str:
    # nginx 등 신뢰 프록시 뒤에 있을 때는 X-Forwarded-For의 첫 IP가 실제 클라이언트다.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def _ip_allowed(ip: str) -> bool:
    if not _ALLOWED_NETWORKS:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _ALLOWED_NETWORKS)


@app.middleware("http")
async def ip_whitelist(request: Request, call_next):
    if _ALLOWED_NETWORKS:
        path = request.url.path
        is_cron = path == "/api/licenses/check-expiry" and request.headers.get("authorization", "").startswith("Bearer ")
        is_exempt = path in _IP_EXEMPT_EXACT or any(path.startswith(p) for p in _IP_EXEMPT_PREFIXES) or is_cron
        if not is_exempt and not _ip_allowed(_client_ip(request)):
            return JSONResponse({"detail": "허용되지 않은 접속 위치입니다"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """정적 파일(HTML/CSS/JS)을 항상 재검증하게 한다.

    개발 중 자주 바뀌는 소규모 프로젝트라 브라우저가 이전 버전을 계속
    캐시해서 수정사항이 반영 안 된 것처럼 보이는 문제가 실제로 있었다.
    """
    response = await call_next(request)
    no_cache_paths = {"/", "/submit", "/track", "/login", "/admin", "/licenses", "/manuals"}
    if request.url.path in no_cache_paths or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# ---------------------------------------------------------------------- #
# 인증
#
# 세션엔 username/role/display_name만 담는다 — 비밀번호 해시 등 민감한 값은
# 절대 세션(쿠키)에 넣지 않는다. 쿠키는 서명되어 있어 변조는 막히지만 암호화는
# 아니라서 내용이 그대로 보인다.

def current_user(request: Request) -> Optional[dict[str, str]]:
    return request.session.get("user")


def require_login(request: Request) -> dict[str, str]:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return user


def require_admin(user: dict[str, str] = Depends(require_login)) -> dict[str, str]:
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="관리자만 가능합니다")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str


class AccountCreate(BaseModel):
    username: str
    password: str
    role: str = ROLE_STAFF
    display_name: Optional[str] = ""


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    account = store.get_account(payload.username)

    locked_until = (account or {}).get("locked_until") or ""
    if locked_until and locked_until > datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
        raise HTTPException(status_code=429, detail=f"로그인 시도가 많아 잠시 잠겼습니다. {locked_until} 이후 다시 시도하세요.")

    password_hash = (account or {}).get("password_hash")
    is_active = (account or {}).get("is_active", 1)
    ok = verify_password(payload.password, password_hash) and bool(is_active)

    if account:
        store.record_login_result(payload.username, success=ok)

    if not ok:
        # 아이디가 없는 경우와 비밀번호가 틀린 경우를 메시지로 구분하지 않는다(계정 존재 여부 노출 방지).
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다")

    request.session.clear()  # 세션 고정(session fixation) 방지 — 로그인 성공 시 세션을 새로 시작
    request.session["user"] = {
        "username": account["username"],
        "role": account["role"],
        "display_name": account.get("display_name") or account["username"],
    }
    return {"username": account["username"], "role": account["role"], "display_name": account.get("display_name") or account["username"]}


@app.post("/api/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(user: dict[str, str] = Depends(require_login)):
    return user


@app.get("/login")
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/")
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/admin")
def admin_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if user.get("role") != ROLE_ADMIN:
        return JSONResponse({"detail": "관리자만 접근할 수 있습니다"}, status_code=403)
    return FileResponse(STATIC_DIR / "admin.html")


@app.post("/api/admin/accounts")
def create_account(payload: AccountCreate, _: dict = Depends(require_admin)):
    err = validate_username(payload.username)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if payload.role not in (ROLE_ADMIN, ROLE_STAFF):
        raise HTTPException(status_code=400, detail=f"role은 {ROLE_ADMIN} 또는 {ROLE_STAFF}만 가능합니다")
    err = validate_password_strength(payload.password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    try:
        return store.create_account(
            username=payload.username,
            password_hash=hash_password(payload.password),
            role=payload.role,
            display_name=payload.display_name or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/admin/accounts")
def list_accounts(_: dict = Depends(require_admin)):
    return store.list_accounts()


class AccountActiveUpdate(BaseModel):
    is_active: bool


class PasswordReset(BaseModel):
    new_password: str


@app.patch("/api/admin/accounts/{username}")
def set_account_active(username: str, payload: AccountActiveUpdate, _: dict = Depends(require_admin)):
    try:
        return store.set_account_active(username, payload.is_active)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/admin/accounts/{username}/unlock")
def unlock_account(username: str, _: dict = Depends(require_admin)):
    try:
        return store.unlock_account(username)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/admin/accounts/{username}/reset-password")
def reset_account_password(username: str, payload: PasswordReset, _: dict = Depends(require_admin)):
    err = validate_password_strength(payload.new_password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    try:
        return store.set_account_password(username, hash_password(payload.new_password))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------- #
# 고객사 / 제품 / 라이선스
#
# 클레임과 달리 보안 민감도가 낮은 운영 데이터라 admin 제한 없이 로그인한
# 누구나(담당자 포함) 관리할 수 있게 열어둔다.


class CustomerCreate(BaseModel):
    name: str
    contact: Optional[str] = ""
    email: Optional[str] = ""
    memo: Optional[str] = ""
    category: Optional[str] = ""  # 구분(예: 코스피, 대학)


class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    contact: Optional[str] = None
    email: Optional[str] = None
    memo: Optional[str] = None
    category: Optional[str] = None


class ProductCreate(BaseModel):
    customer_id: int
    name: str


class ProductUpdate(BaseModel):
    name: str


class LicenseCreate(BaseModel):
    product_id: int
    plan_name: Optional[str] = ""
    start_date: str
    end_date: str
    notify_emails: Optional[str] = ""  # 쉼표로 여러 명 (과거 단일 필드, 분리 입력 전 데이터와의 호환용)
    warning_days: Optional[int] = 30
    notify_staff_emails: Optional[str] = ""
    notify_customer_emails: Optional[str] = ""
    notify_target_option: Optional[str] = "both"
    license_key: Optional[str] = ""


class LicenseUpdate(BaseModel):
    plan_name: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    notify_emails: Optional[str] = None
    warning_days: Optional[int] = None
    notify_staff_emails: Optional[str] = None
    notify_customer_emails: Optional[str] = None
    notify_target_option: Optional[str] = None
    license_key: Optional[str] = None


@app.post("/api/customers")
def create_customer(payload: CustomerCreate, _: dict = Depends(require_login)):
    return store.create_customer(
        payload.name, payload.contact or "", payload.email or "", payload.memo or "", payload.category or ""
    )


@app.get("/api/customers")
def list_customers(_: dict = Depends(require_login)):
    return store.list_customers()


@app.patch("/api/customers/{customer_id}")
def update_customer(customer_id: int, payload: CustomerUpdate, _: dict = Depends(require_login)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        return store.update_customer(customer_id, updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/customers/{customer_id}")
def delete_customer(customer_id: int, _: dict = Depends(require_login)):
    try:
        store.delete_customer(customer_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@app.get("/api/customers/{customer_id}/overview")
def customer_overview(customer_id: int, _: dict = Depends(require_login)):
    try:
        return store.get_customer_overview(customer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/products")
def create_product(payload: ProductCreate, _: dict = Depends(require_login)):
    try:
        return store.create_product(payload.customer_id, payload.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/products")
def list_products(customer_id: Optional[int] = None, _: dict = Depends(require_login)):
    return store.list_products(customer_id=customer_id)


@app.patch("/api/products/{product_id}")
def update_product(product_id: int, payload: ProductUpdate, _: dict = Depends(require_login)):
    try:
        return store.update_product(product_id, payload.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, _: dict = Depends(require_login)):
    try:
        store.delete_product(product_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True}


@app.post("/api/licenses")
def create_license(payload: LicenseCreate, _: dict = Depends(require_login)):
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="만료일은 시작일 이후여야 합니다")
    try:
        return store.create_license(
            payload.product_id, payload.plan_name or "", payload.start_date, payload.end_date,
            payload.notify_emails or "",
            warning_days=payload.warning_days or 30,
            notify_staff_emails=payload.notify_staff_emails or "",
            notify_customer_emails=payload.notify_customer_emails or "",
            notify_target_option=payload.notify_target_option or "both",
            license_key=payload.license_key or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/licenses")
def list_licenses(customer_id: Optional[int] = None, _: dict = Depends(require_login)):
    return store.list_licenses(customer_id=customer_id)


@app.patch("/api/licenses/{license_id}")
def update_license(license_id: int, payload: LicenseUpdate, _: dict = Depends(require_login)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "start_date" in updates and "end_date" in updates and updates["end_date"] < updates["start_date"]:
        raise HTTPException(status_code=400, detail="만료일은 시작일 이후여야 합니다")
    try:
        return store.update_license(license_id, updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/licenses/{license_id}")
def delete_license(license_id: int, _: dict = Depends(require_login)):
    store.delete_license(license_id)
    return {"ok": True}


@app.get("/api/licenses/{license_id}/notification-logs")
def license_notification_logs(license_id: int, _: dict = Depends(require_login)):
    return store.list_license_notification_logs(license_id)


@app.post("/api/licenses/import")
async def import_licenses(
    file: UploadFile = File(...),
    default_product_name: str = "",
    dry_run: bool = False,
    _: dict = Depends(require_login),
):
    """고객사·제품·라이선스를 엑셀/CSV로 일괄 등록한다 (예: 사이트명/라이선스키/구독기간 형식).

    등록된 고객사·제품명과 일치하면 자동으로 연결하고, 없으면 새로 만든다. 같은
    제품에 같은 license_key가 이미 있으면 중복으로 건너뛴다. dry_run=true면 실제로
    DB에 쓰지 않고 인식된 매핑 결과만 미리보기로 돌려준다.
    """
    content = await file.read()
    try:
        ok_rows, failed_rows = license_import.import_rows(file.filename, content, default_product_name=default_product_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    imported_count = 0
    duplicate_rows: list[dict[str, Any]] = []
    warning_rows: list[dict[str, Any]] = []
    preview_rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for mapped in ok_rows:
        warnings = mapped.pop("_warnings", None) or []
        customer_name = mapped["customer"]
        product_name = mapped["product"]
        plan_name = mapped.get("plan_name", "")
        start_date = mapped["start_date"]
        end_date = mapped["end_date"]
        license_key = mapped.get("license_key", "")

        # 라이선스 키가 같으면 같은 라이선스로 보고 건너뛴다 — DB에 이미 있는 키뿐 아니라
        # 같은 파일 안에서 겹치는 키도 (고객사·제품과 무관하게) 한 번만 등록한다.
        if license_key:
            if license_key in seen_keys:
                duplicate_rows.append({"license_key": license_key, "reason": "파일 안에서 라이선스 키가 중복됨"})
                continue
            existing = store.find_license_by_key(license_key)
            if existing:
                duplicate_rows.append({
                    "license_key": license_key,
                    "reason": f"이미 등록된 라이선스 키 ({existing.get('customer_name') or '기존 데이터'})",
                })
                continue
            seen_keys.add(license_key)

        if dry_run:
            preview_rows.append({
                "customer": customer_name, "product": product_name, "plan_name": plan_name,
                "start_date": start_date, "end_date": end_date, "license_key": license_key,
                "warnings": warnings,
            })
            continue

        customer = next((c for c in store.list_customers() if c["name"] == customer_name), None)
        if not customer:
            customer = store.create_customer(customer_name, category=mapped.get("customer_category", ""))
        product = next((p for p in store.list_products(customer_id=customer["id"]) if p["name"] == product_name), None)
        if not product:
            product = store.create_product(customer["id"], product_name)

        try:
            row = store.create_license(
                product["id"], plan_name, start_date, end_date,
                mapped.get("notify_emails", ""),
                warning_days=mapped.get("warning_days", 30),
                license_key=license_key,
            )
            imported_count += 1
            if warnings:
                warning_rows.append({"license_id": row["id"], "warnings": warnings})
        except ValueError as e:
            failed_rows.append({"row": None, "reason": str(e)})

    result = {
        "dry_run": dry_run,
        "total_rows": len(ok_rows) + len(failed_rows),
        "imported_count": imported_count,
        "duplicate_count": len(duplicate_rows),
        "duplicate_rows": duplicate_rows[:50],
        "failed_count": len(failed_rows),
        "failed_rows": failed_rows[:50],
        "warnings": warning_rows[:50],
    }
    if dry_run:
        result["preview_rows"] = preview_rows[:50]
    return result


_LICENSE_STATUS_LABELS = {"구독중": "활성", "만료임박": "만료 임박", "만료": "만료"}


def _plan_years(lic: dict[str, Any]) -> int:
    """구독기간(년): 플랜명이 "N년"이면 그 값, 아니면 시작~만료일 간격으로 추정한다."""
    digits = "".join(ch for ch in (lic.get("plan_name") or "") if ch.isdigit())
    if digits and (lic.get("plan_name") or "").endswith("년"):
        return int(digits)
    try:
        days = (date.fromisoformat(lic["end_date"][:10]) - date.fromisoformat(lic["start_date"][:10])).days
        return max(1, round(days / 365))
    except (ValueError, TypeError):
        return 0


def _license_rows_with_site_info() -> list[dict[str, Any]]:
    categories = {c["id"]: c.get("category", "") for c in store.list_customers()}
    rows = []
    for lic in store.list_licenses():
        rows.append({**lic, "category": categories.get(lic["customer_id"], ""), "plan_years": _plan_years(lic)})
    rows.sort(key=lambda r: (r["customer_name"], r["start_date"]))
    return rows


def _build_site_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = summary.setdefault(r["customer_name"], {
            "site": r["customer_name"], "category": r["category"], "total": 0,
            "active": 0, "expiring": 0, "expired": 0, "y1": 0, "y3": 0, "y5": 0,
        })
        s["total"] += 1
        s[{"구독중": "active", "만료임박": "expiring", "만료": "expired"}.get(r["status"], "active")] += 1
        if r["plan_years"] in (1, 3, 5):
            s[f"y{r['plan_years']}"] += 1
    return list(summary.values())


@app.get("/api/licenses/site-summary")
def license_site_summary(_: dict = Depends(require_login)):
    """사이트(고객사)별 라이선스 수·상태·구독기간 요약 (VMware 라이선스 양식의 '사이트별 요약' 시트)."""
    return _build_site_summary(_license_rows_with_site_info())


@app.get("/api/licenses/export.xlsx")
def export_licenses_xlsx(_: dict = Depends(require_login)):
    """가져오기와 같은 양식(라이선스 목록 + 사이트별 요약 두 시트)으로 내보낸다."""
    from openpyxl import Workbook

    rows = _license_rows_with_site_info()
    wb = Workbook()
    ws = wb.active
    ws.title = "라이선스 목록"
    ws.append(["No", "라이선스 키", "사이트명", "구분", "구독기간(년)", "시작일", "만료일", "남은 일수", "상태"])
    for i, r in enumerate(rows, start=1):
        ws.append([
            i, r.get("license_key", ""), r["customer_name"], r["category"], r["plan_years"],
            r["start_date"], r["end_date"], r["days_left"], _LICENSE_STATUS_LABELS.get(r["status"], r["status"]),
        ])
    ws2 = wb.create_sheet("사이트별 요약")
    ws2.append(["사이트명", "구분", "라이선스 수", "활성", "만료 임박", "만료", "1년", "3년", "5년"])
    for s in _build_site_summary(rows):
        ws2.append([s["site"], s["category"], s["total"], s["active"], s["expiring"], s["expired"], s["y1"], s["y3"], s["y5"]])

    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=license_sites.xlsx"},
    )


@app.get("/api/licenses/expiring")
def list_expiring_licenses(_: dict = Depends(require_login)):
    return store.list_expiring_licenses()


@app.get("/api/licenses/check-expiry")
def check_license_expiry(request: Request):
    """만료임박/만료 라이선스를 점검해 담당자(혹은 고객)에게 메일을 보낸다.

    Vercel Cron(매일 1회, 계정 없이 GET + CRON_SECRET 베어러 토큰으로 호출)이나
    로그인한 사용자가 대시보드에서 수동으로 눌러도 호출할 수 있다.
    """
    auth_header = request.headers.get("authorization", "")
    is_cron = bool(CRON_SECRET) and auth_header == f"Bearer {CRON_SECRET}"
    if not is_cron and not current_user(request):
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return email_notify.check_and_notify(store)


@app.get("/licenses")
def licenses_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "licenses.html")


# ---------------------------------------------------------------------- #
# 내부 매뉴얼(FAQ/트러블슈팅) — 유사 클레임과 함께 상세 모달 추천에 쓰인다.


class ManualCreate(BaseModel):
    title: str
    category: Optional[str] = ""
    product_name: Optional[str] = ""
    keywords: Optional[str] = ""
    content: str


class ManualUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    product_name: Optional[str] = None
    keywords: Optional[str] = None
    content: Optional[str] = None


@app.post("/api/manuals")
def create_manual(payload: ManualCreate, _: dict = Depends(require_login)):
    return store.create_manual(payload.title, payload.category or "", payload.product_name or "", payload.keywords or "", payload.content)


@app.get("/api/manuals")
def list_manuals(q: Optional[str] = None, _: dict = Depends(require_login)):
    return store.list_manuals(q=q)


@app.patch("/api/manuals/{manual_id}")
def update_manual(manual_id: int, payload: ManualUpdate, _: dict = Depends(require_login)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        return store.update_manual(manual_id, updates)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/api/manuals/{manual_id}")
def delete_manual_endpoint(manual_id: int, _: dict = Depends(require_login)):
    store.delete_manual(manual_id)
    return {"ok": True}


@app.get("/manuals")
def manuals_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "manuals.html")


# ---------------------------------------------------------------------- #


class ClaimCreate(BaseModel):
    customer: str
    product: str
    category: Optional[str] = ""
    amount_krw: Optional[float] = 0
    description: str
    assignee: Optional[str] = ""
    channel: Optional[str] = CHANNEL_INTERNAL  # 직원이 통화 내용을 대신 입력하는 창구 (기본값)
    contact: Optional[str] = ""
    processing_type: Optional[str] = ""  # 자체처리/엔지니어방문/협력업체이관
    visit_time: Optional[str] = ""
    resolution: Optional[str] = ""
    difficulty: Optional[str] = ""
    customer_id: Optional[int] = None
    product_id: Optional[int] = None
    license_id: Optional[int] = None
    sub_status: Optional[str] = ""
    processing_minutes: Optional[int] = 0
    partner_name: Optional[str] = ""
    partner_contact: Optional[str] = ""
    scheduled_visit_at: Optional[str] = ""
    completed_visit_at: Optional[str] = ""
    issue_started_at: Optional[str] = ""  # 장애 발생시점 (달력 입력)
    issue_ended_at: Optional[str] = ""  # 장애 종료시점


class PublicClaimCreate(BaseModel):
    """고객이 웹/앱에서 직접 접수하는 창구 (로그인 불필요)."""

    channel: str = CHANNEL_WEB  # 웹 폼이면 "웹", 앱 SDK가 호출하면 "앱"으로 지정
    customer: str
    contact: str
    product: str
    description: str
    amount_krw: Optional[float] = 0


class SmsInbound(BaseModel):
    """문자 수신 연동용 웹훅 payload.

    실제 알림톡/문자 API(예: 알리고, NHN Cloud 등) 연동 시 해당 업체의
    콜백 payload 형식에 맞춰 이 모델을 조정하면 된다. 지금은 발신번호와
    본문만 받는 최소 스키마다.
    """

    from_number: str
    text: str


class ClaimUpdate(BaseModel):
    status: Optional[str] = None
    assignee: Optional[str] = None
    category: Optional[str] = None
    amount_krw: Optional[float] = None
    description: Optional[str] = None
    processing_type: Optional[str] = None
    visit_time: Optional[str] = None
    resolution: Optional[str] = None
    difficulty: Optional[str] = None
    customer_id: Optional[int] = None
    product_id: Optional[int] = None
    license_id: Optional[int] = None
    sub_status: Optional[str] = None
    processing_minutes: Optional[int] = None
    partner_name: Optional[str] = None
    partner_contact: Optional[str] = None
    scheduled_visit_at: Optional[str] = None
    completed_visit_at: Optional[str] = None
    issue_started_at: Optional[str] = None
    issue_ended_at: Optional[str] = None
    note: Optional[str] = None  # 상태 변경/재오픈 사유 등 — claim_history에만 남고 클레임 필드로는 저장 안 됨


@app.get("/")
def root(request: Request):
    if not current_user(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/submit")
def submit_page():
    """고객이 직접 접수하는 공개 웹 폼 (앱 웹뷰에서도 그대로 재사용 가능)."""
    return FileResponse(STATIC_DIR / "submit.html")


@app.get("/track")
def track_page():
    """고객이 접수번호+연락처로 진행 상황을 조회하는 공개 페이지."""
    return FileResponse(STATIC_DIR / "submit.html")


def _require_fields(data: dict[str, Any], labels: dict[str, str]) -> None:
    missing = [label for key, label in labels.items() if not str(data.get(key) or "").strip()]
    if missing:
        raise HTTPException(status_code=400, detail=f"필수 항목을 입력해주세요: {', '.join(missing)}")
    for key in labels:
        data[key] = str(data[key]).strip()


def _check_issue_period(started: Optional[str], ended: Optional[str]) -> None:
    if started and ended and ended < started:
        raise HTTPException(status_code=400, detail="장애 종료시점이 발생시점보다 빠릅니다")


@app.post("/api/claims")
def create_claim(payload: ClaimCreate, _: dict = Depends(require_login)):
    """내부 직원용 접수 (전화 응대 내용을 대신 입력하는 등).

    필수 항목(고객사명/제품/담당자명/장애 내용)이 비어 있으면 등록하지 않는다 — 화면의
    required 속성만 믿지 않고 서버에서도 막는다(공백만 입력한 경우 포함).
    """
    data = payload.model_dump()
    _require_fields(data, {"customer": "고객사명", "product": "제품", "assignee": "담당자명", "description": "장애 내용"})
    _check_issue_period(data.get("issue_started_at"), data.get("issue_ended_at"))
    row = store.create_claim(data)
    return row


@app.post("/api/public/claims")
def create_public_claim(payload: PublicClaimCreate):
    """고객이 웹/앱에서 직접 접수. 같은 상태 파이프라인(접수→처리중→완료)에 합류한다."""
    if payload.channel not in (CHANNEL_WEB, CHANNEL_APP):
        raise HTTPException(status_code=400, detail="channel은 웹 또는 앱만 가능합니다")
    data = payload.model_dump()
    data["category"] = ""
    row = store.create_claim(data)
    return row


@app.post("/api/channels/sms")
def create_claim_from_sms(payload: SmsInbound):
    """문자(SMS) 착신 웹훅. 통신/문자 API 게이트웨이가 이 엔드포인트를 호출하도록 등록한다."""
    row = store.create_claim(
        {
            "channel": CHANNEL_SMS,
            "customer": payload.from_number,
            "contact": payload.from_number,
            "product": "",
            "description": payload.text,
        }
    )
    return row


# ---------------------------------------------------------------------- #
# 고객용 티켓 조회/추적 (로그인 불필요)
#
# 접수번호만으로는 조회가 안 되게 접수 시 입력한 연락처로 본인 확인을 한다.
# 존재하지 않는 접수번호와 연락처 불일치를 응답으로 구분하지 않는다(계정 로그인과
# 같은 이유 — 어느 접수번호가 실재하는지 외부에 노출하지 않기 위함).
def _contact_matches(stored_contact: str, provided_contact: str) -> bool:
    stored_digits = "".join(ch for ch in (stored_contact or "") if ch.isdigit())
    provided_digits = "".join(ch for ch in (provided_contact or "") if ch.isdigit())
    if not stored_digits or len(provided_digits) < 4:
        return False
    return stored_digits == provided_digits or stored_digits.endswith(provided_digits)


def _get_claim_for_customer(claim_id: str, contact: str) -> dict[str, Any]:
    try:
        claim = store.get_claim(claim_id)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="접수번호 또는 연락처를 확인해주세요")
    if not _contact_matches(claim.get("contact", ""), contact):
        raise HTTPException(status_code=404, detail="접수번호 또는 연락처를 확인해주세요")
    return claim


class PublicTrackRequest(BaseModel):
    claim_id: str
    contact: str


class PublicNoteCreate(BaseModel):
    contact: str
    body: str


@app.post("/api/public/claims/track")
def track_public_claim(payload: PublicTrackRequest):
    """접수번호 + 연락처로 본인 확인 후 진행 상황만 돌려준다.

    내부 전용 정보(담당자 실명, 원가, 타 고객사 데이터 등)는 응답에서 제외한다 —
    고객에게는 "배정 여부"만 보여주고 담당자 이름은 내보내지 않는다.
    """
    claim = _get_claim_for_customer(payload.claim_id, payload.contact)
    return {
        "claim_id": claim["claim_id"],
        "status": claim["status"],
        "sub_status": claim.get("sub_status", ""),
        "assignee_assigned": bool(claim.get("assignee")),
        "scheduled_visit_at": claim.get("scheduled_visit_at", ""),
        "resolution": claim.get("resolution", "") if claim["status"] == STATUS_DONE else "",
        "created_at": claim["created_at"],
        "updated_at": claim["updated_at"],
        "notes": store.list_customer_notes(claim["claim_id"]),
        "attachments": store.list_attachments(claim["claim_id"]),
    }


@app.post("/api/public/claims/{claim_id}/attachments")
async def upload_public_attachment(claim_id: str, contact: str = Form(...), file: UploadFile = File(...)):
    claim = _get_claim_for_customer(claim_id, contact)
    if len(store.list_attachments(claim_id)) >= 3:
        raise HTTPException(status_code=400, detail="첨부파일은 최대 3개까지 등록할 수 있습니다")
    return await _save_attachment(claim_id, file)


@app.post("/api/public/claims/{claim_id}/notes")
def add_public_note(claim_id: str, payload: PublicNoteCreate):
    """완료 전 클레임에 대해 고객이 추가 증상이나 보충 설명을 남길 수 있다."""
    claim = _get_claim_for_customer(claim_id, payload.contact)
    if claim["status"] == STATUS_DONE:
        raise HTTPException(status_code=400, detail="이미 완료된 클레임에는 추가 등록할 수 없습니다")
    return store.add_customer_note(claim_id, payload.body)


def _resolve_customer_product(customer_name: str, product_name: str, auto_create: bool) -> tuple[Optional[int], Optional[int]]:
    """고객사/제품명이 이미 등록되어 있으면 그 id로 연결하고, auto_create가 True면
    등록되어 있지 않은 고객사/제품을 그 자리에서 새로 만든다."""
    customer = next((c for c in store.list_customers() if c["name"] == customer_name), None)
    if not customer and auto_create:
        customer = store.create_customer(customer_name)
    if not customer:
        return None, None
    product = next((p for p in store.list_products(customer_id=customer["id"]) if p["name"] == product_name), None)
    if not product and auto_create:
        product = store.create_product(customer["id"], product_name)
    return customer["id"], (product["id"] if product else None)


@app.post("/api/claims/import")
async def import_claims(
    file: UploadFile = File(...),
    dry_run: bool = False,
    auto_create_master: bool = False,
    _: dict = Depends(require_login),
):
    """기업이 기존에 엑셀/CSV로 갖고 있던 클레임 이력을 일괄 등록한다.

    파일 자체가 문제(형식/용량/인코딩/손상)면 400으로 즉시 실패한다.
    행 단위 문제는 성공/중복/실패로 분류해서 보고하며, 한 행이 실패해도
    나머지 행 처리는 계속된다. dry_run=true면 실제로 DB에 쓰지 않고
    인식된 매핑·예상 등록/중복 건수만 미리보기로 돌려준다.
    """
    content = await file.read()
    if license_import.looks_like_license_file(file.filename, content):
        raise HTTPException(
            status_code=400,
            detail="라이선스 목록 양식으로 보입니다. '가져올 데이터 종류'를 '라이선스(사이트별)'로 선택해 다시 올려주세요.",
        )
    try:
        ok_rows, file_duplicates, failed_rows = bulk_import.import_rows(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    imported_count = 0
    warning_rows: list[dict[str, Any]] = []
    preview_rows: list[dict[str, Any]] = []
    duplicate_rows = [{"row": d["row"], "reason": d["reason"]} for d in file_duplicates]

    for mapped in ok_rows:
        warnings = mapped.pop("_warnings", None) or []

        if mapped.get("customer") and mapped.get("product"):
            customer_id, product_id = _resolve_customer_product(mapped["customer"], mapped["product"], auto_create_master)
            if customer_id:
                mapped["customer_id"] = customer_id
            if product_id:
                mapped["product_id"] = product_id

        if dry_run:
            existing = store.find_duplicate(
                mapped.get("customer", ""), mapped.get("product", ""), mapped.get("description", ""),
                mapped.get("created_at") or "",
            )
            if existing:
                duplicate_rows.append({"reason": f"기존 데이터와 동일 ({existing})"})
            else:
                preview_rows.append({
                    "customer": mapped.get("customer"), "product": mapped.get("product"),
                    "description": mapped.get("description"), "status": mapped.get("status", "접수"),
                    "warnings": warnings,
                })
            continue

        try:
            row = store.import_claim(mapped)
            imported_count += 1
            if warnings:
                warning_rows.append({"claim_id": row["claim_id"], "warnings": warnings})
        except DuplicateClaim as e:
            duplicate_rows.append({"reason": f"기존 데이터와 동일 ({e.existing_claim_id})"})
        except (ValueError, sqlite3.Error, psycopg.Error) as e:
            failed_rows.append({"row": None, "reason": str(e)})

    result = {
        "dry_run": dry_run,
        "total_rows": len(ok_rows) + len(duplicate_rows) + len(failed_rows),
        "imported_count": imported_count,
        "duplicate_count": len(duplicate_rows),
        "failed_count": len(failed_rows),
        "duplicate_rows": duplicate_rows[:50],
        "failed_rows": [{"row": f["row"], "reason": f["reason"]} for f in failed_rows[:50]],
        "warnings": warning_rows[:50],
    }
    if dry_run:
        result["preview_rows"] = preview_rows[:50]
    return result


@app.get("/api/claims")
def list_claims(
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    channel: Optional[str] = None,
    contact: Optional[str] = None,
    q: Optional[str] = None,
    difficulty: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    _: dict = Depends(require_login),
):
    """date_from/date_to(YYYY-MM-DD)는 장애 발생시점(없으면 접수일) 기준 기간 검색."""
    return store.list_claims(
        status=status, assignee=assignee, channel=channel, contact=contact, q=q, difficulty=difficulty,
        date_from=date_from, date_to=date_to,
    )


@app.get("/api/claims/{claim_id}")
def get_claim(claim_id: str, _: dict = Depends(require_login)):
    try:
        return store.get_claim(claim_id)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="클레임을 찾을 수 없습니다")


@app.patch("/api/claims/{claim_id}")
def update_claim(claim_id: str, payload: ClaimUpdate, user: dict = Depends(require_login)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    _check_issue_period(updates.get("issue_started_at"), updates.get("issue_ended_at"))
    updates["changed_by"] = user.get("display_name") or user.get("username") or ""
    try:
        return store.update_claim(claim_id, updates)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="클레임을 찾을 수 없습니다")
    except (InvalidTransition, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/claims/{claim_id}/history")
def claim_history(claim_id: str, _: dict = Depends(require_login)):
    try:
        store.get_claim(claim_id)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="클레임을 찾을 수 없습니다")
    return store.list_claim_history(claim_id)


@app.get("/api/claims/{claim_id}/recommendations")
def claim_recommendations(claim_id: str, _: dict = Depends(require_login)):
    try:
        current = store.get_claim(claim_id)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="클레임을 찾을 수 없습니다")
    similar = store.find_similar_claims(claim_id)
    manual_query = current.get("product") or current.get("category") or ""
    manuals = store.list_manuals(q=manual_query)[:3] if manual_query else []
    return {"similar_claims": similar, "manuals": manuals}


@app.get("/api/stats/summary")
def stats_summary(_: dict = Depends(require_login)):
    return store.stats_summary()


@app.get("/api/stats/analytics")
def stats_analytics(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    customer_id: Optional[int] = None,
    _: dict = Depends(require_login),
):
    """담당자별/처리유형별/난이도별/고객사별 실적 집계. SQLite·Postgres 어느 쪽이든
    list_claims()가 반환하는 공통 dict 리스트 위에서 순수 Python으로 집계하므로
    두 백엔드에 각각 SQL을 따로 둘 필요가 없다."""
    claims = store.list_claims()
    if customer_id:
        claims = [c for c in claims if c.get("customer_id") == customer_id]
    if from_date:
        claims = [c for c in claims if c["created_at"][:10] >= from_date]
    if to_date:
        claims = [c for c in claims if c["created_at"][:10] <= to_date]

    def avg(vals: list[float]) -> float:
        vals = [v for v in vals if v]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    by_assignee: dict[str, dict[str, Any]] = {}
    for c in claims:
        name = c.get("assignee") or "미배정"
        bucket = by_assignee.setdefault(
            name,
            {"received": 0, "in_progress": 0, "done": 0, "minutes": [], "by_difficulty": {"low": 0, "medium": 0, "high": 0}},
        )
        if c["status"] == "접수":
            bucket["received"] += 1
        elif c["status"] == "처리중":
            bucket["in_progress"] += 1
        elif c["status"] == "완료":
            bucket["done"] += 1
            if c.get("processing_minutes"):
                bucket["minutes"].append(c["processing_minutes"])
            if c.get("difficulty") in bucket["by_difficulty"]:
                bucket["by_difficulty"][c["difficulty"]] += 1

    by_assignee_out = []
    for name, b in by_assignee.items():
        total = b["received"] + b["in_progress"] + b["done"]
        by_assignee_out.append({
            "assignee": name,
            "received": b["received"],
            "in_progress": b["in_progress"],
            "done": b["done"],
            "completion_rate": round(b["done"] / total * 100, 1) if total else 0.0,
            "avg_processing_minutes": avg(b["minutes"]),
            "by_difficulty": b["by_difficulty"],
        })
    by_assignee_out.sort(key=lambda x: -x["done"])

    by_processing_type: dict[str, int] = {}
    for c in claims:
        key = c.get("processing_type") or "미정"
        by_processing_type[key] = by_processing_type.get(key, 0) + 1

    difficulty_buckets: dict[str, dict[str, Any]] = {}
    for c in claims:
        if c["status"] != "완료":
            continue
        key = c.get("difficulty") or "미정"
        d = difficulty_buckets.setdefault(key, {"count": 0, "minutes": []})
        d["count"] += 1
        if c.get("processing_minutes"):
            d["minutes"].append(c["processing_minutes"])
    by_difficulty = {k: {"count": v["count"], "avg_processing_minutes": avg(v["minutes"])} for k, v in difficulty_buckets.items()}

    by_category: dict[str, int] = {}
    for c in claims:
        key = c.get("category") or "미분류"
        by_category[key] = by_category.get(key, 0) + 1

    customer_counts: dict[str, int] = {}
    for c in claims:
        key = c.get("customer") or "미상"
        customer_counts[key] = customer_counts.get(key, 0) + 1
    by_customer_top = [
        {"customer": k, "count": v} for k, v in sorted(customer_counts.items(), key=lambda kv: -kv[1])[:10]
    ]

    # 최근 6개월 접수/완료 추이 (데이터가 없는 달도 0으로 채워 그래프가 끊기지 않게 한다)
    today = datetime.now()
    months: list[str] = []
    y, m = today.year, today.month
    for i in range(5, -1, -1):
        mm, yy = m - i, y
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append(f"{yy:04d}-{mm:02d}")
    monthly = {mo: {"received": 0, "done": 0} for mo in months}
    for c in claims:
        mo = c["created_at"][:7]
        if mo in monthly:
            monthly[mo]["received"] += 1
        if c["status"] == "완료" and c.get("completed_at"):
            mo2 = c["completed_at"][:7]
            if mo2 in monthly:
                monthly[mo2]["done"] += 1
    monthly_trend = [{"month": mo, **monthly[mo]} for mo in months]

    return {
        "by_assignee": by_assignee_out,
        "by_processing_type": by_processing_type,
        "by_difficulty": by_difficulty,
        "by_category": by_category,
        "by_customer_top": by_customer_top,
        "monthly_trend": monthly_trend,
    }


@app.get("/api/stats/export.csv")
def stats_export_csv(_: dict = Depends(require_login)):
    claims = store.list_claims()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "접수번호", "접수일시", "채널", "고객사", "제품", "유형", "상태", "담당자",
        "처리유형", "난이도", "처리소요분(분)", "처리결과",
    ])
    for c in claims:
        writer.writerow([
            c.get("claim_id"), c.get("created_at"), c.get("channel"), c.get("customer"), c.get("product"),
            c.get("category"), c.get("status"), c.get("assignee"), c.get("processing_type"),
            c.get("difficulty"), c.get("processing_minutes"), c.get("resolution"),
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=claims_export.csv"},
    )


# ---------------------------------------------------------------------- #
# 클레임 첨부파일 (이미지/영상)
#
# 저장 파일명은 업로드 원본 파일명을 그대로 쓰지 않고 uuid4로 새로 만든다 —
# 원본 파일명에 경로 조작 문자(../ 등)가 섞여 있어도 디스크 경로에 영향을 주지
# 않게 하기 위함. 원본 파일명은 메타데이터(표시용)로만 DB에 남긴다.
async def _save_attachment(claim_id: str, file: UploadFile) -> dict[str, Any]:
    """클레임 존재 확인부터 디스크 저장·DB 메타데이터 기록까지 — 내부/공개 업로드
    엔드포인트가 공통으로 쓴다. 클레임 미존재는 ClaimNotFound를 그대로 전파한다."""
    store.get_claim(claim_id)  # 없으면 ClaimNotFound

    content_type = file.content_type or ""
    if content_type not in ALLOWED_ATTACHMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 파일 형식입니다: {content_type or '알수없음'}")

    content = await file.read()
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=400, detail=f"파일 용량은 최대 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB까지 가능합니다")

    ext = Path(file.filename or "").suffix[:10]  # 확장자만 표시용으로 보존, 나머지는 uuid
    stored_name = f"{uuid.uuid4().hex}{ext}"
    attachment_storage.save(claim_id, stored_name, content, content_type)

    return store.add_attachment(
        claim_id=claim_id,
        filename=file.filename or stored_name,
        stored_name=stored_name,
        content_type=content_type,
        size_bytes=len(content),
    )


@app.post("/api/claims/{claim_id}/attachments")
async def upload_attachment(claim_id: str, file: UploadFile = File(...), _: dict = Depends(require_login)):
    try:
        return await _save_attachment(claim_id, file)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="클레임을 찾을 수 없습니다")


@app.get("/api/claims/{claim_id}/attachments")
def list_attachments(claim_id: str, _: dict = Depends(require_login)):
    return store.list_attachments(claim_id)


@app.get("/api/attachments/{attachment_id}/file")
def get_attachment_file(attachment_id: int, _: dict = Depends(require_login)):
    att = store.get_attachment(attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다")
    kind, value = attachment_storage.get_file_or_url(att["claim_id"], att["stored_name"])
    if kind == "path":
        if not Path(value).is_file():
            raise HTTPException(status_code=404, detail="첨부파일이 서버에 남아있지 않습니다")
        return FileResponse(value, media_type=att["content_type"] or None, filename=att["filename"])
    return RedirectResponse(value)  # S3 등 외부 스토리지 — 서명된 다운로드 URL로 리다이렉트


@app.delete("/api/attachments/{attachment_id}")
def delete_attachment(attachment_id: int, _: dict = Depends(require_login)):
    att = store.delete_attachment(attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다")
    attachment_storage.delete(att["claim_id"], att["stored_name"])
    return {"ok": True}
