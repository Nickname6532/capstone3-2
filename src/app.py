"""
고객 클레임 접수/처리 통합관리 시스템 - FastAPI 백엔드.

실행:
    .venv/bin/uvicorn app:app --reload --port 8000
    (src/ 디렉터리에서 실행)
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Optional

import psycopg
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import bulk_import
from claim_logger import ClaimLogger
from claims_store import (
    CHANNEL_APP,
    CHANNEL_INTERNAL,
    CHANNEL_SMS,
    CHANNEL_WEB,
    ClaimNotFound,
    ClaimsStore,
    DuplicateClaim,
    InvalidTransition,
)

app = FastAPI(title="클레임 통합관리 시스템")

BASE_DIR = Path(__file__).resolve().parent

# Vercel 같은 서버리스 환경은 배포 번들이 읽기 전용이고 /tmp만 쓰기 가능하다.
# 그 /tmp도 요청마다 다른 인스턴스에 뜰 수 있어 데이터가 언제든 초기화될 수 있으니,
# DATABASE_URL이 있으면 Postgres를 원본으로 쓰고, 없으면(로컬 개발) SQLite를 쓴다.
if os.environ.get("VERCEL"):
    DATA_ROOT = Path(tempfile.gettempdir())
else:
    DATA_ROOT = BASE_DIR

# Vercel의 Supabase 연동(Storage 탭에서 붙이는 방식)은 DATABASE_URL이 아니라
# POSTGRES_URL / POSTGRES_PRISMA_URL 같은 이름으로 환경변수를 넣어준다. DATABASE_URL만
# 보고 있으면 Supabase를 붙여도 계속 SQLite(=/tmp, 휘발성)로 폴백되어 데이터가
# 그대로 사라진다 — 실제로 겪었던 문제라 여러 이름을 다 확인한다.
DATABASE_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
    or os.environ.get("POSTGRES_URL_NON_POOLING")
    or os.environ.get("POSTGRES_PRISMA_URL")
)
if DATABASE_URL:
    from claims_store_pg import PgClaimsStore

    logger = None  # Postgres 백엔드는 CSV 이력 없이 claims 테이블에서 직접 통계를 집계한다
    store = PgClaimsStore(database_url=DATABASE_URL)
else:
    logger = ClaimLogger(base_dir=DATA_ROOT / "logs")
    store = ClaimsStore(base_dir=DATA_ROOT / "data", logger=logger)

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """정적 파일(HTML/CSS/JS)을 항상 재검증하게 한다.

    개발 중 자주 바뀌는 소규모 프로젝트라 브라우저가 이전 버전을 계속
    캐시해서 수정사항이 반영 안 된 것처럼 보이는 문제가 실제로 있었다.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/") or request.url.path == "/submit":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


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
def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/submit")
def submit_page():
    """고객이 직접 접수하는 공개 웹 폼 (앱 웹뷰에서도 그대로 재사용 가능)."""
    return FileResponse(STATIC_DIR / "submit.html")


@app.post("/api/claims")
def create_claim(payload: ClaimCreate):
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
async def import_claims(file: UploadFile = File(...)):
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
):
    return store.list_claims(status=status, assignee=assignee, channel=channel, contact=contact, q=q)


@app.get("/api/claims/{claim_id}")
def get_claim(claim_id: str):
    try:
        return store.get_claim(claim_id)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="클레임을 찾을 수 없습니다")


@app.patch("/api/claims/{claim_id}")
def update_claim(claim_id: str, payload: ClaimUpdate):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        return store.update_claim(claim_id, updates)
    except ClaimNotFound:
        raise HTTPException(status_code=404, detail="클레임을 찾을 수 없습니다")
    except InvalidTransition as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/stats/summary")
def stats_summary():
    return store.stats_summary()
