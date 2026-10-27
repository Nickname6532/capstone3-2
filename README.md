# 클레임 통합관리 시스템

고객 클레임 접수부터 처리 결과까지 한 곳에서 관리하는 시스템. 구두 보고로만 오가던 클레임 처리 현황을
접수(내부입력/웹/앱/문자) → 상태 추적(접수/처리중/완료) → 결과 이력(일/월/년 CSV)으로 정리한다.

## 구조

- `src/app.py` — FastAPI 백엔드 (REST API + 정적 대시보드 서빙)
- `src/claims_store.py` — 클레임 상태 마스터 테이블 (SQLite, `data/claims.db`)
- `src/claim_logger.py` — 완료된 클레임을 일/월/년 CSV로 적재 (리포팅·통계용, 마스터 데이터는 아님)
- `src/static/` — 내부 대시보드(`index.html`) + 고객 접수 폼(`submit.html`)
- `api/index.py`, `vercel.json` — Vercel 서버리스 배포용 진입점
- `Dockerfile`, `docker-compose.yml`, `nginx/` — Docker + nginx로 상시 서버 배포할 때 쓰는 구성

## Docker로 실행 (nginx 리버스 프록시 포함)

```bash
docker compose up -d --build
```

nginx가 80(컨테이너 기준, 호스트는 `8080`)에서 `/`, `/submit`, `/static`, `/api/*`를 모두 받아
FastAPI 컨테이너로 리버스 프록시한다. http://localhost:8080 으로 접속.

외부에 임시로 공개하려면(예: 데모):

```bash
cloudflared tunnel --url http://localhost:8080
```

계정 없이도 `https://<임의문자열>.trycloudflare.com` 주소가 바로 발급된다. 다만 이 임시 터널은
Cloudflare가 가동을 보장하지 않으니 중요한 시연 전에는 미리 접속 테스트를 해볼 것.

## 로그인/계정

내부 대시보드(`/`, `/api/claims*`, `/api/stats/summary`)는 로그인해야 접근할 수 있다. 고객용
`/submit`, `POST /api/public/claims`, `POST /api/channels/sms`는 그대로 로그인 없이 열려 있다.

**최초 계정 만들기** — 코드/깃에 기본 비밀번호를 남기지 않도록, 반드시 터미널에서 직접 만든다:

```bash
cd src
../.venv/bin/python create_admin.py
```

아이디·권한(admin/staff)·비밀번호를 그 자리에서 입력받는다(비밀번호는 화면에 표시되지 않음).
이후 추가 계정은 로그인한 admin 계정으로 `/admin` 페이지에서 만들면 된다.

**보안 설계**
- 비밀번호는 bcrypt로만 해시해서 저장한다(평문·MD5·SHA1 금지). API 응답에는 어떤 경우에도
  `password_hash`가 섞여 나가지 않는다.
- 로그인 실패 시 "아이디 없음"과 "비밀번호 틀림"을 구분하지 않는다(계정 존재 여부 노출 방지).
  존재하지 않는 아이디로 시도해도 더미 해시로 검증해 응답 시간이 비슷하게 맞춰진다.
- 같은 계정으로 5회 연속 로그인 실패하면 15분간 잠긴다(무차별 대입 방지). 이 잠금 상태는
  DB에 저장되므로 서버리스 인스턴스가 바뀌어도 유지된다.
- 세션 쿠키는 서명(HttpOnly)되고 `SameSite=Strict`로 막혀 있어 CSRF에 안전하고, 로그인 성공 시
  세션을 새로 발급한다(세션 고정 공격 방지).
- `SESSION_SECRET` 환경변수를 반드시 고정값으로 설정할 것 — **설정하지 않으면 서버가 뜰 때마다
  임의의 키를 새로 생성하는데, Vercel처럼 인스턴스가 여러 개 뜨는 환경에서는 인스턴스마다 키가
  달라서 로그인이 수시로 풀린다.** 로컬 개발은 신경 쓰지 않아도 되지만(프로세스 하나만 뜨므로),
  Vercel에는 꼭 등록한다:
  ```bash
  python3 -c "import secrets; print(secrets.token_hex(32))"
  ```
  로 생성한 값을 Vercel 프로젝트 환경변수 `SESSION_SECRET`에 넣는다.

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
