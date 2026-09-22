"""
고객 클레임 접수/처리 통합관리 시스템 - FastAPI 백엔드.

실행:
    .venv/bin/uvicorn app:app --reload --port 8000
    (src/ 디렉터리에서 실행)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from claim_logger import ClaimLogger
from claims_store import CHANNEL_APP, CHANNEL_INTERNAL, CHANNEL_SMS, CHANNEL_WEB, ClaimNotFound, ClaimsStore, InvalidTransition

app = FastAPI(title="클레임 통합관리 시스템")

BASE_DIR = Path(__file__).resolve().parent

# Vercel 같은 서버리스 환경은 배포 번들이 읽기 전용이고 /tmp만 쓰기 가능하다.
# 그 /tmp도 요청마다 다른 인스턴스에 뜰 수 있어 데이터가 언제든 초기화될 수 있으니,
# 실사용이 아니라 데모/테스트 용도로만 이 경로를 쓴다는 점을 감안해야 한다.
if os.environ.get("VERCEL"):
    DATA_ROOT = Path(tempfile.gettempdir())
else:
    DATA_ROOT = BASE_DIR

store = ClaimsStore(base_dir=DATA_ROOT / "data")
logger = ClaimLogger(base_dir=DATA_ROOT / "logs")

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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


@app.get("/api/claims")
def list_claims(
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    channel: Optional[str] = None,
    q: Optional[str] = None,
):
    return store.list_claims(status=status, assignee=assignee, channel=channel, q=q)


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
    claims = store.list_claims()
    open_count = sum(1 for c in claims if c["status"] != "완료")
    in_progress = sum(1 for c in claims if c["status"] == "처리중")
    received = sum(1 for c in claims if c["status"] == "접수")
    done = sum(1 for c in claims if c["status"] == "완료")

    monthly_rows = []
    monthly_path = logger.monthly_dir
    if monthly_path.exists():
        files = sorted(monthly_path.glob("*_summary.csv"), reverse=True)
        if files:
            import csv as _csv

            with files[0].open("r", newline="", encoding="utf-8-sig") as f:
                monthly_rows = list(_csv.DictReader(f))

    this_month_count = sum(int(r.get("claim_count") or 0) for r in monthly_rows)
    this_month_cost = sum(float(r.get("total_cost_krw") or 0) for r in monthly_rows)

    channel_breakdown: dict[str, int] = {}
    for c in claims:
        ch = c.get("channel") or "내부입력"
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
