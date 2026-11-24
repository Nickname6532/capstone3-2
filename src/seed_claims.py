"""등록된 라이선스를 기준으로 테스트용 클레임 데이터를 만든다.

라이선스마다 상태(활성/만료 임박/만료)에 어울리는 문의를 0~2건 생성한다. 클레임은
고객사·제품·라이선스(customer_id/product_id/license_id)에 연결되고, 설명 앞에 [테스트]
표식과 라이선스 키를 붙여 나중에 구분/검색할 수 있다. 같은 시드(--seed)면 항상 같은 결과다.

실행(DB에 직접 생성 — 라이선스가 먼저 등록돼 있어야 함):
    cd src
    ../.venv/bin/python seed_claims.py                # 이미 [테스트] 클레임이 있으면 건너뜀
    ../.venv/bin/python seed_claims.py --seed 7       # 다른 조합

실행(웹 화면 "기존 데이터 가져오기 → 클레임 이력"로 올릴 CSV 생성 — 라이선스 엑셀/CSV 기반):
    ../.venv/bin/python seed_claims.py --from-file ~/Downloads/vmware_license_sites.xlsx --csv ../sample_data/test_claims.csv
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

TEST_MARK = "[테스트]"

ASSIGNEES = ["김도현", "이서연", "박민수", ""]
CHANNELS = ["내부입력", "웹", "앱", "문자", "음성"]
PROCESSING_TYPES = ["자체처리", "엔지니어방문", "협력업체이관"]
DIFFICULTIES = ["low", "medium", "high"]

# 라이선스 상태별로 어울리는 (원인 유형, 문의 내용) 후보.
SCENARIOS: dict[str, list[tuple[str, str]]] = {
    "활성": [
        ("로그인/인증 오류", "라이선스 키 입력 후에도 활성화 인증이 실패한다는 문의입니다."),
        ("기타", "호스트(CPU) 추가 시 라이선스 수량이 맞는지 확인 요청."),
        ("서비스 장애(다운타임)", "vCenter 연결 후 라이선스 정보가 '평가판'으로 표시됩니다."),
        ("API 오류", "라이선스 사용량 조회 API가 간헐적으로 오류를 반환합니다."),
        ("기타", "구독 기간/조건을 증빙 문서로 받고 싶다는 요청입니다."),
    ],
    "만료 임박": [
        ("결제/청구 오류", "곧 만료되는 라이선스 갱신 견적서를 요청합니다."),
        ("기타", "갱신 시 구독 기간을 3년으로 변경 가능한지 문의합니다."),
        ("결제/청구 오류", "갱신 결제를 진행했는데 만료일이 연장되지 않았습니다."),
        ("기타", "만료 임박 알림 메일을 담당자 추가 수신으로 변경해 주세요."),
    ],
    "만료": [
        ("서비스 장애(다운타임)", "라이선스 만료 후 일부 기능이 제한되었습니다. 긴급 연장 요청."),
        ("결제/청구 오류", "만료된 라이선스 재활성화 및 소급 청구 문의입니다."),
        ("로그인/인증 오류", "만료된 키로 인증이 거부됩니다. 신규 키 발급이 필요합니다."),
    ],
}

# 상태별 문의 건수(0/1/2건) 가중치 — 만료/임박일수록 문의가 많다.
COUNT_WEIGHTS = {"활성": [5, 4, 1], "만료 임박": [2, 5, 3], "만료": [2, 4, 4]}

RESOLUTIONS = [
    "라이선스 키 재입력 및 활성화 서버 연결 확인 후 정상 처리했습니다.",
    "갱신 견적서 발송 및 만료일 연장 반영을 완료했습니다.",
    "고객사 방화벽 설정 안내 후 인증이 정상화되었습니다.",
    "신규 라이선스 키를 발급해 적용했습니다.",
]


def _license_status(end_date: str, today: date) -> str:
    left = (date.fromisoformat(end_date[:10]) - today).days
    if left < 0:
        return "만료"
    return "만료 임박" if left <= 90 else "활성"


def build_claim_specs(licenses: list[dict[str, Any]], seed: int = 42, today: date | None = None) -> list[dict[str, Any]]:
    """라이선스 목록 → 클레임 명세 목록.

    각 라이선스 dict는 customer, product, license_key, end_date(YYYY-MM-DD)가 필요하고,
    DB에서 온 경우 customer_id/product_id/license_id도 그대로 넘겨준다.
    """
    today = today or date.today()
    rng = random.Random(seed)
    specs: list[dict[str, Any]] = []

    for lic in licenses:
        status = _license_status(lic["end_date"], today)
        count = rng.choices([0, 1, 2], weights=COUNT_WEIGHTS[status])[0]
        # 한 라이선스 안에서는 서로 다른 문의를 고른다(같은 내용이 두 번 들어가 중복이 되지 않게).
        for category, text in rng.sample(SCENARIOS[status], k=count):
            roll = rng.random()
            days_ago = rng.randint(0, 60)
            key = lic.get("license_key") or ""
            spec = {
                "customer": lic["customer"],
                "product": lic["product"],
                "contact": f"010-{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}",
                "channel": rng.choice(CHANNELS),
                "category": category,
                "description": f"{TEST_MARK} {text}" + (f" (라이선스 키: {key})" if key else ""),
                "assignee": rng.choice(ASSIGNEES) if roll > 0.25 else "",
                "created_on": (today - timedelta(days=days_ago)).isoformat(),
                "final_status": "접수" if roll <= 0.34 else ("처리중" if roll <= 0.67 else "완료"),
                "processing_type": rng.choice(PROCESSING_TYPES) if roll > 0.3 else "",
                "difficulty": rng.choice(DIFFICULTIES) if roll > 0.3 else "",
                "processing_minutes": rng.choice([15, 30, 45, 60, 90]) if roll > 0.67 else 0,
                "resolution": rng.choice(RESOLUTIONS) if roll > 0.67 else "",
            }
            for id_key in ("customer_id", "product_id", "license_id"):
                if lic.get(id_key):
                    spec[id_key] = lic[id_key]
            specs.append(spec)
    return specs


def seed_into_store(store, seed: int = 42) -> int:
    """DB에 등록된 라이선스를 기준으로 클레임을 만든다. 만든 건수를 반환."""
    if store.list_claims(q=TEST_MARK):
        print("이미 [테스트] 클레임이 있어 건너뜁니다.")
        return 0
    licenses = [
        {
            "customer": lic["customer_name"], "product": lic["product_name"],
            "license_key": lic.get("license_key", ""), "end_date": lic["end_date"],
            "customer_id": lic["customer_id"], "product_id": lic["product_id"], "license_id": lic["id"],
        }
        for lic in store.list_licenses()
    ]
    if not licenses:
        print("등록된 라이선스가 없습니다. 먼저 라이선스를 가져오거나 seed_demo.py를 실행하세요.")
        return 0

    created = 0
    for spec in build_claim_specs(licenses, seed=seed):
        data = {k: v for k, v in spec.items() if k not in ("created_on", "final_status", "resolution")}
        row = store.create_claim(data)
        if spec["final_status"] in ("처리중", "완료"):
            store.update_claim(row["claim_id"], {"status": "처리중", "changed_by": "seed_claims"})
        if spec["final_status"] == "완료":
            store.update_claim(
                row["claim_id"], {"status": "완료", "changed_by": "seed_claims", "resolution": spec["resolution"]}
            )
        created += 1
    print(f"라이선스 {len(licenses)}건 기준 테스트 클레임 {created}건 생성됨.")
    return created


CSV_HEADERS = ["접수일", "채널", "고객사", "연락처", "제품", "유형", "내용", "담당자", "난이도", "상태", "처리유형", "처리결과"]


def write_csv(specs: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig: 엑셀에서 한글이 깨지지 않게
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        for s in specs:
            writer.writerow([
                f"{s['created_on']} 10:00", s["channel"], s["customer"], s["contact"], s["product"],
                s["category"], s["description"], s["assignee"], s["difficulty"], s["final_status"],
                s["processing_type"], s["resolution"],
            ])


def load_licenses_from_file(path: Path) -> list[dict[str, Any]]:
    import license_import

    rows, failed = license_import.import_rows(path.name, path.read_bytes(), default_product_name="VMware")
    if failed:
        print(f"형식 문제로 건너뛴 행 {len(failed)}건", file=sys.stderr)
    return [
        {"customer": r["customer"], "product": r["product"], "license_key": r.get("license_key", ""), "end_date": r["end_date"]}
        for r in rows
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="라이선스 기반 테스트 클레임 생성")
    parser.add_argument("--seed", type=int, default=42, help="같은 값이면 같은 결과")
    parser.add_argument("--from-file", type=Path, help="라이선스 엑셀/CSV (DB 대신 파일을 기준으로 사용)")
    parser.add_argument("--csv", type=Path, help="DB 대신 클레임 CSV 파일로 저장")
    args = parser.parse_args()

    if args.csv:
        if not args.from_file:
            parser.error("--csv는 --from-file(라이선스 파일)과 함께 써야 합니다")
        specs = build_claim_specs(load_licenses_from_file(args.from_file), seed=args.seed)
        write_csv(specs, args.csv)
        print(f"{len(specs)}건을 {args.csv}에 저장했습니다.")
    else:
        from db import build_store

        store, _ = build_store()
        seed_into_store(store, seed=args.seed)
