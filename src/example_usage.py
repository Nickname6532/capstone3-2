"""claim_logger.py 사용 예시.

실제 파이프라인에서는 STT/LLM 호출 결과를 record dict로 만들어
logger.log_claim(record)만 호출하면 daily/monthly/yearly CSV가 자동 갱신된다.
"""

from datetime import datetime

from claim_logger import ClaimLogger

logger = ClaimLogger(base_dir="logs")

sample_claims = [
    {
        "claim_id": "CLM-20260922-001",
        "customer": "OO전자",
        "product": "3상 인버터 모듈",
        "category": "부품불량",
        "amount_krw": 1200000,
        "stt_text": "고객님께서 인버터 모듈에서 이상 소음이 난다고 하셨습니다.",
        "structured_json": {"원인": "부품불량", "심각도": "중", "조치": "부품 교체"},
        "tokens_input": 3800,
        "tokens_output": 400,
        "cost_krw": 24,
        "status": "OK",
    },
    {
        "claim_id": "CLM-20260922-002",
        "customer": "XX산업",
        "product": "A-2301 로트",
        "category": "배송지연",
        "amount_krw": 0,
        "stt_text": "납기가 일주일 지연되었다는 클레임입니다.",
        "structured_json": {"원인": "배송지연", "심각도": "하", "조치": "일정 재조율"},
        "tokens_input": 1250,
        "tokens_output": 350,
        "cost_krw": 4,
        "status": "OK",
    },
]

for claim in sample_claims:
    logger.log_claim(claim, when=datetime(2026, 9, 22, 14, 30))

print("완료: logs/daily, logs/monthly, logs/yearly 확인")
