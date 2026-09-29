"""Vercel의 Supabase 연동이 넣어주는 연결 문자열에 섞인 미지원 쿼리 파라미터를
걸러내는지 검증한다. DB 연결이 필요 없어 항상 실행된다(로컬 Postgres 없어도 통과).
"""

import pytest

pytest.importorskip("psycopg")

from claims_store_pg import _sanitize_conninfo


def test_strips_unknown_supabase_metadata_param():
    url = "postgresql://user:pw@host:5432/db?supa=abcdef&sslmode=require"
    result = _sanitize_conninfo(url)
    assert "supa" not in result
    assert "sslmode=require" in result


def test_keeps_known_libpq_params():
    url = "postgresql://user:pw@host:6543/db?sslmode=require&connect_timeout=10&application_name=app"
    result = _sanitize_conninfo(url)
    assert "sslmode=require" in result
    assert "connect_timeout=10" in result
    assert "application_name=app" in result


def test_url_without_query_is_unchanged_in_shape():
    url = "postgresql://user:pw@host:5432/db"
    result = _sanitize_conninfo(url)
    assert result.startswith("postgresql://user:pw@host:5432/db")
