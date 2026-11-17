"""시연용 가상 데이터 일괄 생성 스크립트.

실행:
    cd src
    ../.venv/bin/python seed_demo.py           # 이미 시드 데이터가 있으면 건너뜀
    ../.venv/bin/python seed_demo.py --reset   # 기존 고객사/제품/라이선스를 지우고 다시 만듦

build_store()를 그대로 써서 app.py/create_admin.py와 항상 같은 DB를 보도록 한다.
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta

from claims_store import CHANNEL_APP, CHANNEL_INTERNAL, CHANNEL_SMS, CHANNEL_VOICE, CHANNEL_WEB, STATUS_DONE, STATUS_IN_PROGRESS
from db import build_store

CUSTOMERS = [
    {"name": "넷스퀘어", "contact": "02-555-1000", "email": "billing@netsquare.io"},
    {"name": "브릿지랩스", "contact": "02-555-2000", "email": "ops@bridgelabs.io"},
    {"name": "모아테크", "contact": "02-555-3000", "email": "cs@moatech.io"},
    {"name": "케이클라우드", "contact": "02-555-4000", "email": "support@kcloud.io"},
    {"name": "데이터브릿지", "contact": "02-555-5000", "email": "noc@databridge.io"},
    {"name": "클라우드인프라팀", "contact": "02-555-6000", "email": "infra@cloudinfra.io"},
]

PRODUCT_NAMES = ["Pro 플랜", "Enterprise 플랜", "Storage API", "Sync API", "Team 플랜", "Basic 플랜"]

CATEGORIES = ["로그인/인증 오류", "결제/청구 오류", "서비스 장애(다운타임)", "속도저하/성능", "데이터 동기화 오류", "API 오류", "기타"]
CHANNELS = [CHANNEL_INTERNAL, CHANNEL_WEB, CHANNEL_APP, CHANNEL_SMS, CHANNEL_VOICE]
PROCESSING_TYPES = ["자체처리", "엔지니어방문", "협력업체이관"]
DIFFICULTIES = ["low", "medium", "high"]
ASSIGNEES = ["김도현", "이서연", "박민수", ""]

DESCRIPTIONS = [
    "SSO 로그인 시도 시 500 에러가 반복됩니다.",
    "이번 달 청구서에 동일 금액이 두 번 청구되었습니다.",
    "오전 업무 시간대에 업로드 API가 전부 타임아웃났습니다.",
    "대시보드 로딩이 평소보다 느립니다.",
    "외부 DB와의 동기화 작업이 실패 상태로 멈춰 있습니다.",
    "API 응답이 간헐적으로 503으로 떨어집니다.",
    "플랜 변경 후 일부 기능이 비활성화된 것처럼 보입니다.",
]


def _iso(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).isoformat()


def seed(reset: bool = False, store=None) -> None:
    if store is None:
        store, _logger = build_store()
    already_seeded = not reset and bool(store.list_customers())

    if reset:
        for c in store.list_customers():
            for p in store.list_products(customer_id=c["id"]):
                for lic in store.list_licenses(customer_id=c["id"]):
                    if lic["product_id"] == p["id"]:
                        store.delete_license(lic["id"])
                store.delete_product(p["id"])
            store.delete_customer(c["id"])

    if already_seeded:
        print("이미 시드 데이터가 있어 건너뜁니다 (다시 만들려면 --reset).")
        return

    license_plan_count = 0
    for c in CUSTOMERS:
        customer = store.create_customer(c["name"], c["contact"], c["email"])
        product_count = random.randint(2, 3)
        for product_name in random.sample(PRODUCT_NAMES, product_count):
            product = store.create_product(customer["id"], product_name)
            # 라이선스 상태를 구독중/만료임박(D-15,D-5)/만료 섞어서 만든다.
            bucket = license_plan_count % 4
            if bucket == 0:
                start, end = _iso(-200), _iso(300)  # 구독중
            elif bucket == 1:
                start, end = _iso(-300), _iso(15)  # 만료임박 D-15
            elif bucket == 2:
                start, end = _iso(-300), _iso(5)  # 만료임박 D-5
            else:
                start, end = _iso(-400), _iso(-10)  # 만료됨
            store.create_license(
                product["id"], random.choice(["Standard", "Pro", "Enterprise"]), start, end,
                notify_emails=c["email"], warning_days=30,
                notify_staff_emails="ops@company.com", notify_customer_emails=c["email"],
            )
            license_plan_count += 1
    print(f"고객사 {len(CUSTOMERS)}곳, 라이선스 {license_plan_count}건 생성됨.")

    claim_count = 30
    created = 0
    for _ in range(claim_count):
        customer = random.choice(CUSTOMERS)
        product = random.choice(PRODUCT_NAMES)
        status_roll = random.random()
        row = store.create_claim({
            "customer": customer["name"],
            "contact": f"010-{random.randint(1000,9999)}-{random.randint(1000,9999)}",
            "product": product,
            "category": random.choice(CATEGORIES),
            "description": random.choice(DESCRIPTIONS),
            "channel": random.choice(CHANNELS),
            "assignee": random.choice(ASSIGNEES),
            "processing_type": random.choice(PROCESSING_TYPES) if status_roll > 0.3 else "",
            "difficulty": random.choice(DIFFICULTIES) if status_roll > 0.3 else "",
            "processing_minutes": random.choice([15, 30, 45, 60, 90, 120]) if status_roll > 0.3 else 0,
        })
        if status_roll > 0.33:
            store.update_claim(row["claim_id"], {"status": STATUS_IN_PROGRESS, "changed_by": "seed_demo"})
        if status_roll > 0.66:
            store.update_claim(row["claim_id"], {
                "status": STATUS_DONE, "changed_by": "seed_demo",
                "resolution": "원인 확인 후 설정 초기화로 해결했습니다.",
            })
        created += 1

    print(f"클레임 {created}건 생성됨 (접수/처리중/완료 비율 혼합).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="시연용 가상 데이터 생성")
    parser.add_argument("--reset", action="store_true", help="기존 고객사/제품/라이선스를 지우고 다시 만든다")
    args = parser.parse_args()
    seed(reset=args.reset)
