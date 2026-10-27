"""
최초 관리자 계정을 만드는 스크립트. 소스코드/깃에 기본 비밀번호를 남기지 않기 위해
반드시 터미널에서 직접 실행해서 그 자리에서 입력받는다 (비밀번호는 화면에 echo되지 않음).

실행 (로컬 SQLite):
    cd src
    ../.venv/bin/python create_admin.py

실행 (Postgres, 배포 DB에 직접 만들 때):
    DATABASE_URL="postgresql://..." ../.venv/bin/python create_admin.py
"""

from __future__ import annotations

import getpass
import sys

from auth import ROLE_ADMIN, ROLE_STAFF, hash_password, validate_password_strength, validate_username
from db import build_store


def main() -> int:
    print("=== 관리자/직원 계정 생성 ===")
    username = input("아이디: ").strip()
    err = validate_username(username)
    if err:
        print(f"오류: {err}")
        return 1

    role_input = input("권한 (admin/staff, 기본 admin): ").strip().lower() or ROLE_ADMIN
    if role_input not in (ROLE_ADMIN, ROLE_STAFF):
        print(f"오류: role은 {ROLE_ADMIN} 또는 {ROLE_STAFF}만 가능합니다.")
        return 1

    display_name = input("표시 이름 (선택): ").strip()

    password = getpass.getpass("비밀번호 (입력 시 화면에 표시되지 않음): ")
    err = validate_password_strength(password)
    if err:
        print(f"오류: {err}")
        return 1
    password_confirm = getpass.getpass("비밀번호 확인: ")
    if password != password_confirm:
        print("오류: 비밀번호가 일치하지 않습니다.")
        return 1

    store, _ = build_store()
    try:
        account = store.create_account(
            username=username,
            password_hash=hash_password(password),
            role=role_input,
            display_name=display_name,
        )
    except ValueError as e:
        print(f"오류: {e}")
        return 1

    print(f"생성 완료: {account['username']} ({account['role']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
