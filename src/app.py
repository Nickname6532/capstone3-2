"""
고객 클레임 접수/처리 통합관리 시스템 - FastAPI 백엔드.

실행:
    .venv/bin/uvicorn app:app --reload --port 8000
    (src/ 디렉터리에서 실행)

로그인: 최초 관리자 계정은 create_admin.py로 터미널에서 직접 만든다(코드에
기본 비밀번호를 남기지 않기 위함). README의 "로그인/계정" 절 참고.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import psycopg
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import bulk_import
from auth import (
    ROLE_ADMIN,
    ROLE_STAFF,
    hash_password,
    validate_password_strength,
    validate_username,
    verify_password,
)
from claims_store import CHANNEL_APP, CHANNEL_INTERNAL, CHANNEL_SMS, CHANNEL_WEB, ClaimNotFound, DuplicateClaim, InvalidTransition
from db import build_store, get_database_url

app = FastAPI(title="클레임 통합관리 시스템")

BASE_DIR = Path(__file__).resolve().parent
IS_VERCEL = bool(os.environ.get("VERCEL"))

store, logger = build_store()

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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


@app.middleware("http")
async def no_cache_static(request, call_next):
    """정적 파일(HTML/CSS/JS)을 항상 재검증하게 한다.

    개발 중 자주 바뀌는 소규모 프로젝트라 브라우저가 이전 버전을 계속
    캐시해서 수정사항이 반영 안 된 것처럼 보이는 문제가 실제로 있었다.
    """
    response = await call_next(request)
    no_cache_paths = {"/", "/submit", "/login", "/admin"}
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


@app.get("/")
def root(request: Request):
    if not current_user(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/submit")
def submit_page():
    """고객이 직접 접수하는 공개 웹 폼 (앱 웹뷰에서도 그대로 재사용 가능)."""
    return FileResponse(STATIC_DIR / "submit.html")


@app.post("/api/claims")
def create_claim(payload: ClaimCreate, _: dict = Depends(require_login)):
    """내부 직원용 접수 (전화 응대 내용을 대신 입력하는 등)."""
    row = store.create_claim(payload.model_dump())
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


@app.post("/api/claims/import")
async def import_claims(file: UploadFile = File(...), _: dict = Depends(require_login)):
    """기업이 기존에 엑셀/CSV로 갖고 있던 클레임 이력을 일괄 등록한다.

    파일 자체가 문제(형식/용량/인코딩/손상)면 400으로 즉시 실패한다.
    행 단위 문제는 성공/중복/실패로 분류해서 보고하며, 한 행이 실패해도
    나머지 행 처리는 계속된다.
    """
    content = await file.read()
    try:
        ok_rows, file_duplicates, failed_rows = bulk_import.import_rows(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    imported_count = 0
    warning_rows: list[dict[str, Any]] = []
    duplicate_rows = [{"row": d["row"], "reason": d["reason"]} for d in file_duplicates]

    for i, mapped in enumerate(ok_rows, start=1):
        warnings = mapped.pop("_warnings", None)
        try:
            row = store.import_claim(mapped)
            imported_count += 1
            if warnings:
                warning_rows.append({"claim_id": row["claim_id"], "warnings": warnings})
        except DuplicateClaim as e:
            duplicate_rows.append({"reason": f"기존 데이터와 동일 ({e.existing_claim_id})"})
        except (ValueError, sqlite3.Error, psycopg.Error) as e:
            failed_rows.append({"row": None, "reason": str(e)})

    return {
        "total_rows": len(ok_rows) + len(duplicate_rows) + len(failed_rows),
        "imported_count": imported_count,
        "duplicate_count": len(duplicate_rows),
        "failed_count": len(failed_rows),
        "duplicate_rows": duplicate_rows[:50],
        "failed_rows": [{"row": f["row"], "reason": f["reason"]} for f in failed_rows[:50]],
        "warnings": warning_rows[:50],
    }


@app.get("/api/claims")
def list_claims(
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    channel: Optional[str] = None,
    contact: Optional[str] = None,
    q: Optional[str] = None,
    _: dict = Depends(require_login),
):
    return store.list_claims(status=status, assignee=assignee, channel=channel, contact=contact, q=q)


@app.get("/api/claims/{claim_id}")
def get_claim(claim_id: str, _: dict = Depends(require_login)):
    try:
        return store.get_claim(claim_id)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="클레임을 찾을 수 없습니다")


@app.patch("/api/claims/{claim_id}")
def update_claim(claim_id: str, payload: ClaimUpdate, _: dict = Depends(require_login)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        return store.update_claim(claim_id, updates)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="클레임을 찾을 수 없습니다")
    except InvalidTransition as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/stats/summary")
def stats_summary(_: dict = Depends(require_login)):
    return store.stats_summary()
