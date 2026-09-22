# 클레임 통합관리 시스템

고객 클레임 접수부터 처리 결과까지 한 곳에서 관리하는 시스템. 구두 보고로만 오가던 클레임 처리 현황을
접수(내부입력/웹/앱/문자) → 상태 추적(접수/처리중/완료) → 결과 이력(일/월/년 CSV)으로 정리한다.

## 로컬 실행

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd src
../.venv/bin/uvicorn app:app --reload --port 8000
```

- 내부 대시보드: http://localhost:8000
- 고객 접수 폼: http://localhost:8000/submit

## 구조

- `src/app.py` — FastAPI 백엔드 (REST API + 정적 대시보드 서빙)
- `src/claims_store.py` — 클레임 상태 마스터 테이블 (`data/claims.csv`)
- `src/claim_logger.py` — 완료된 클레임을 일/월/년 CSV로 적재
- `src/static/` — 내부 대시보드(`index.html`) + 고객 접수 폼(`submit.html`)
- `api/index.py`, `vercel.json` — Vercel 서버리스 배포용 진입점

## 접수 채널

| 채널 | 경로 |
|---|---|
| 내부입력 | 대시보드 접수 폼 |
| 웹/앱 | `/submit` (공개 폼), `POST /api/public/claims` |
| 문자(SMS) | `POST /api/channels/sms` (웹훅) |

## Vercel 배포 시 주의

Vercel 서버리스 환경은 배포 파일이 읽기 전용이고 `/tmp`만 쓰기 가능하다. 이 프로젝트는 `VERCEL` 환경변수가
있으면 데이터를 `/tmp`에 저장하도록 자동 전환하지만, `/tmp`는 인스턴스마다 다르고 언제든 초기화될 수 있다.
즉 **Vercel 배포본은 데이터가 영구 보존되지 않는 테스트/데모용**이다. 실사용 시에는 별도 DB(예: Vercel
Postgres, Supabase)나 상시 서버 배포가 필요하다.
