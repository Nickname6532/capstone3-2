"""
앱 본체(app.py)와 관리자 생성 스크립트(create_admin.py)가 반드시 같은 데이터베이스를
보도록 저장소 선택 로직을 한 곳에 모은다. 예전에 이 로직이 두 파일에 따로 있었는데,
환경변수 인식 방식이 하나라도 어긋나면 관리자 계정을 엉뚱한 DB에 만드는 사고로
이어질 수 있어 공용 모듈로 뺐다.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent


def get_data_root() -> Path:
    # Vercel 같은 서버리스 환경은 배포 번들이 읽기 전용이고 /tmp만 쓰기 가능하다.
    if os.environ.get("VERCEL"):
        return Path(tempfile.gettempdir())
    return BASE_DIR


def get_database_url() -> Optional[str]:
    # Vercel의 Supabase/Neon 연동이 실제로 쓰는 환경변수 이름이 제각각이라 순서대로 확인한다.
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("POSTGRES_URL_NON_POOLING")
        or os.environ.get("POSTGRES_PRISMA_URL")
    )


def build_store():
    """(store, logger) 를 반환한다. Postgres 백엔드면 logger는 None."""
    database_url = get_database_url()
    if database_url:
        from claims_store_pg import PgClaimsStore

        return PgClaimsStore(database_url=database_url), None

    from claim_logger import ClaimLogger
    from claims_store import ClaimsStore

    data_root = get_data_root()
    logger = ClaimLogger(base_dir=data_root / "logs")
    store = ClaimsStore(base_dir=data_root / "data", logger=logger)
    return store, logger
