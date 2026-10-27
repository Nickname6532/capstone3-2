"""
비밀번호 해싱/검증과 계정 관련 정책. 세션·쿠키 처리는 app.py에서
Starlette SessionMiddleware로 한다(여기서는 암호학적 자재만 다룸).

지키는 것:
- 비밀번호는 절대 평문/약한 해시(MD5, SHA1, 단순 SHA256)로 저장하지 않는다 — bcrypt만 쓴다.
- "아이디가 존재하지 않음"과 "비밀번호가 틀림"을 응답 메시지와 타이밍 둘 다에서 구분하지 않는다
  (계정 존재 여부가 드러나는 건 그 자체로 정보 노출이다 — 존재하지 않는 아이디는 더 빨리
  응답이 와서 유효한 아이디 목록을 추측당하는 사고가 실제로 흔하다).
"""

from __future__ import annotations

import re
import secrets

import bcrypt

ROLE_ADMIN = "admin"
ROLE_STAFF = "staff"
VALID_ROLES = {ROLE_ADMIN, ROLE_STAFF}

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.]{3,32}$")

# 존재하지 않는 아이디로 로그인 시도했을 때도 실제 검증과 비슷한 시간이 걸리도록
# 더미 해시를 미리 한 번 계산해둔다(타이밍 사이드채널로 계정 존재 여부가 드러나는 것 방지).
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """password_hash가 None이면(계정 없음) 더미 해시로 검증해 타이밍을 맞춘다."""
    target = password_hash.encode("ascii") if password_hash else _DUMMY_HASH
    try:
        return bcrypt.checkpw(password.encode("utf-8"), target) and password_hash is not None
    except ValueError:
        # 저장된 해시가 손상된 경우(이론상) — 손상된 해시로 통과시키는 사고를 막는다.
        return False


def validate_username(username: str) -> str | None:
    if not _USERNAME_RE.match(username or ""):
        return "아이디는 영문/숫자/._ 3~32자여야 합니다."
    return None


def validate_password_strength(password: str) -> str | None:
    if not password or len(password) < 10:
        return "비밀번호는 10자 이상이어야 합니다."
    if password.isdigit() or password.isalpha():
        return "비밀번호는 숫자와 문자를 함께 포함해야 합니다."
    if password.lower() in {"password", "123456789", "qwertyuiop"}:
        return "너무 흔한 비밀번호입니다."
    return None
