# 클레임 통합관리 시스템

고객 클레임 접수부터 처리 결과까지 한 곳에서 관리하는 시스템. 구두 보고로만 오가던 클레임 처리 현황을
접수(내부입력/웹/앱/문자) → 상태 추적(접수/처리중/완료) → 결과 이력(일/월/년 CSV)으로 정리한다.

## 구조

- `src/app.py` — FastAPI 백엔드 (REST API + 정적 대시보드 서빙)
- `src/claims_store.py` — 클레임 상태 마스터 테이블 (SQLite, `data/claims.db`)
- `src/claim_logger.py` — 완료된 클레임을 일/월/년 CSV로 적재 (리포팅·통계용, 마스터 데이터는 아님)
- `src/static/` — 내부 대시보드(`index.html`) + 고객 접수 폼(`submit.html`)
- `api/index.py`, `vercel.json` — Vercel 서버리스 배포용 진입점

## 접수 채널

| 채널 | 경로 |
|---|---|
| 내부입력 | 대시보드 접수 폼 |
| 웹/앱 | `/submit` (공개 폼), `POST /api/public/claims` |
| 문자(SMS) | `POST /api/channels/sms` (웹훅) |

## 저장소

`DATABASE_URL`(없으면 `POSTGRES_URL` → `POSTGRES_URL_NON_POOLING` → `POSTGRES_PRISMA_URL` 순으로 확인,
Vercel의 Supabase/Neon 연동이 실제로 쓰는 이름들) 환경변수 유무로 백엔드가 자동으로 갈린다.

- **없음 (로컬 개발 기본값)** — SQLite(`data/claims.db`). 여러 담당자가 동시에 접속해도 파일 전체를
  다시 쓰던 CSV 방식과 달리 행 단위 트랜잭션으로 안전하게 갱신된다. 이전에 CSV로 쓰던 데이터
  (`data/claims.csv`)가 있으면 최초 실행 시 자동으로 SQLite로 옮기고 `claims.csv.migrated`로 이름을 바꿔
  보관한다. 완료된 클레임의 일/월/년 통계는 `logs/` 아래 CSV로 쌓인다(리포팅·익스포트 전용).
- **있음** — `src/claims_store_pg.py`의 Postgres 백엔드. 완료 이력 통계도 CSV가 아니라 `claims` 테이블에서
  직접 SQL로 집계한다(서버리스 인스턴스가 바뀌어도 값이 유지되도록).

## Vercel 배포 시 주의 — 반드시 Postgres를 붙여야 데이터가 유지된다

Vercel 서버리스 환경은 배포 파일이 읽기 전용이고 `/tmp`만 쓰기 가능한데, 그 `/tmp`도 인스턴스마다
다르고 언제든 초기화된다. **Postgres 연동이 안 되어 있으면 SQLite 파일이 `/tmp`에 저장되어, 요청이
다른 인스턴스로 가거나 인스턴스가 재활용되는 순간 클레임 목록이 사라진다** — 실제로 시연 중 겪었던
문제가 이것이다.

**해결: Vercel 프로젝트에 Postgres를 붙인다 (Supabase 또는 Neon 둘 다 됨).**

1. Vercel 대시보드 → 해당 프로젝트 → **Storage** 탭 → **Create Database** → **Supabase**(또는 Neon) 선택
2. 연결하면 프로젝트 환경변수에 관련 변수가 자동으로 추가된다 — Supabase는 `POSTGRES_URL` 계열 이름을
   쓰고 Neon은 `DATABASE_URL`을 쓰는데, 앱이 둘 다 자동으로 찾는다. 별도로 값을 복사해 넣을 필요 없음
3. 재배포(Redeploy)하면 앱이 기동 시 자동으로 `claims` 테이블을 만들고 Postgres를 원본으로 사용한다
4. 이후로는 인스턴스가 바뀌거나 콜드 스타트가 나도 클레임 데이터가 유지된다

**연동했는데도 데이터가 계속 사라진다면**: Vercel 프로젝트 Settings → Environment Variables에서
`POSTGRES_URL`(또는 `DATABASE_URL`)이 실제로 등록되어 있는지, 그리고 **Production** 환경에도 체크되어
있는지 확인한다. Preview에만 연결돼 있으면 실제 배포 도메인(Production)은 여전히 SQLite로 폴백된다.

로컬에서 Postgres 백엔드를 직접 테스트하려면:

```bash
createdb claim_test
DATABASE_URL="postgresql://localhost:5432/claim_test" .venv/bin/uvicorn app:app --port 8000
```

여러 담당자가 실제로 동시 사용하는 단계에서도 이 Postgres 백엔드를 그대로 쓰면 된다 — 상시 서버로
옮기더라도 `DATABASE_URL`만 그대로 두면 코드 변경 없이 동작한다.
