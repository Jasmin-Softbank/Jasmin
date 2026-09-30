# 데이터베이스: 운영 전략과 마이그레이션 시나리오 (초안)

> 상태: 초안 (2026-09-30). 베스트 프랙티스가 아니라 PoC에서 확인할 설계와 시험 항목이다.

## 근거: 2026 킥오프 원본 발표 (운영진 배포 자료, 2026-09-28)

- "평소 Web 애플리케이션을 배포할 때 고려하는 점": "CI/CD와는 어떻게 연동하는가? **데이터베이스 마이그레이션은 어떻게 진행하는가?**"
- "코드를 자동으로 변경하면서 적절하게 인프라 환경까지 구축해 주는 시스템을 만들어 보세요"
- "간단한 배포"의 예: "간편하게 작성된 애플리케이션이라도 운영 수준의 인프라 환경에 손쉽게 배포 가능하다: SQLite → 적절한 설정이 적용된 RDS"

RAILSHOT은 이 예를 **관리형 RDS가 아니라 클러스터 안의 컨테이너 Postgres(CloudNativePG)**로 구현한다.

## 1. 결정: RDS 대신 컨테이너 Postgres (CloudNativePG)

| 항목 | 내용 |
|---|---|
| 이유 | AWS와 온프렘이 같은 매니페스트·같은 이미지를 쓴다(환경 차이 흡수가 심사 항목). 추가 AWS 의존과 프로비저닝 시간(RDS 생성 수 분)이 없다. 계정·마이그레이션·백업을 선언형으로 한곳에서 관리한다 |
| 대가 | 백업·복구·업그레이드를 우리가 운영한다. 단일 노드라 DB HA는 MVP 밖이다 |
| 클라우드 고유 기능 | 볼륨은 EBS gp3(EBS CSI 드라이버), 백업 저장소는 S3(버전 관리 + 수명 주기), 암호화는 KMS. 온프렘 거울상은 local-path(또는 Longhorn)와 MinIO |
| 버전 | CloudNativePG 1.30.1(지원 K8s 1.34–1.36, PG 14–18), Barman Cloud 플러그인 v0.15.0, PostgreSQL 17 |

## 2. 이미지 세트

| 용도 | 이미지 | 비고 |
|---|---|---|
| 운영 DB | `ghcr.io/cloudnative-pg/postgresql:17` | CNPG 오퍼랜드 이미지(공식 Postgres 기반). digest로 고정 |
| 게이트 임시 DB | `postgres:17` (Docker 공식) | L3 전에 `migrate.command`를 시험하는 일회성 컨테이너. 운영 DB와 같은 메이저 |
| SQLite 이전 (P2) | 앱 이미지 + 우리 이전 스크립트(Python 표준 `sqlite3` → `psql`) | 외부 도구를 들이지 않는다. pgloader는 2022년 이후 릴리스가 없어 쓰지 않는다 |
| 백업 | Barman Cloud 플러그인 v0.15.0 | 내장 `barmanObjectStore` 방식은 1.26부터 폐기 예정이라 쓰지 않는다 |
| 마이그레이션 Job | 앱 이미지 그대로 | `jasmin.yaml`의 `migrate.command` |

## 3. 계정(역할) 전략

앱마다 CNPG `Cluster` 하나, 데이터베이스 `app` 하나. 역할은 셋이다.

| 역할 | 만드는 곳 | 권한 | 쓰는 곳 | 자격이 들어가는 곳 |
|---|---|---|---|---|
| 소유자 `app` | CNPG 부트스트랩(initdb)이 생성 | DB 소유(DDL) | **마이그레이션 Job만** | CNPG가 만드는 `<cluster>-app` Secret → `MIGRATION_DATABASE_URL` |
| 런타임 `<app>_rw` | `DatabaseRole` CRD(`login: true`, `superuser: false`, 회수 정책 `retain`) | 테이블 DML, 시퀀스 사용 | 앱 파드 | ESO Password 생성기 → Secret(사용자·비밀번호·URI 템플릿) → `DATABASE_URL` |
| 읽기 전용 `<app>_ro` | `DatabaseRole`, `inRoles: [pg_monitor]` | SELECT | 진단·관측 | 운영자 전용 Secret |

- `postgres` superuser는 앱에 주지 않는다(CNPG 기본 `enableSuperuserAccess: false`).
- 런타임 역할의 권한은 마이그레이션 Job 첫 단계가 소유자 권한으로 멱등하게 준다(`GRANT USAGE ON SCHEMA`, `ALTER DEFAULT PRIVILEGES ... TO <app>_rw`). 앱 마이그레이션은 그다음에 돈다.
- 비밀번호는 Git에 두지 않는다. 클러스터 안에서 생성되고, 사용자에게도 보여 주지 않는다.
- 비밀번호 교체는 ESO 생성기 갱신 → 앱 롤링 재시작이다(P2).

## 4. 생명주기

| 단계 | 방법 |
|---|---|
| 생성 | 렌더러가 `jasmin.yaml`의 `resources.postgres`에서 `Cluster`(인스턴스 1, 1Gi, requests 100m/256Mi) + `Database` + `DatabaseRole` + `ExternalSecret`을 만든다. 앱과 같은 테넌트 네임스페이스 |
| 마이그레이션 | Argo CD **Sync 단계 hook Job, sync-wave 1**(DB `Cluster`는 wave -1, 앱은 wave 2. PreSync로 두면 첫 배포에서 DB보다 먼저 돌아 실패한다. 재시도 0, 제한 5분, `hook-delete-policy: HookSucceeded`로 실패 Job은 증거로 남김)이 권한 부여 SQL → `migrate.command`를 소유자 자격으로 실행. 도구 감지 순서: alembic, prisma, django, rails, golang-migrate, flyway, `migrations/*.sql`(플랫폼 러너) |
| 게이트 시험 | 임시 `postgres:17`에 같은 순서로 실행해 실패하면 운영 DB는 건드리지 않는다(F9) |
| 백업 | `ScheduledBackup` 하루 1회, 보존 7일. 파괴적 변경 전에 즉석 `Backup`을 만들고 ID를 evidence에 기록 |
| 복구 | 새 `Cluster`를 `bootstrap.recovery`로 만든다(시점 복구). 기존 클러스터를 덮어쓰지 않는다 |
| 삭제 | spec에서 빠지면 분리 후 7일 보존. `Cluster`에 Argo `Prune=false`·`Delete=false`, `DatabaseRole` 회수 정책 `retain`. 실제 삭제는 백업 확인 뒤 파괴적 변경으로만 |
| 업그레이드 | 마이너는 이미지 digest 교체(롤링). 메이저는 MVP 밖 |
| 관측 | CNPG 메트릭(포트 9187)을 Prometheus가 수집, Postgres 로그(JSON stdout)는 Loki로 |
| 연결 풀 | CNPG `Pooler`(PgBouncer)는 P2 |

앱이 받는 환경 변수는 `DATABASE_URL`(런타임 역할) 하나다. 마이그레이션 Job에만 `MIGRATION_DATABASE_URL`(소유자)을 준다.

## 5. 변경 원칙

- 확장 → 이전 → 축소(expand–migrate–contract). 새 컬럼은 nullable로 먼저 추가하고, 삭제는 새 버전이 정상인 뒤 별도 변경으로 한다.
- 앱을 롤백해도 DB는 되돌리지 않는다(업계 공통). 그래서 이전 버전이 새 스키마에서도 동작해야 한다.
- 마이그레이션 동시 실행은 Argo의 단일 동기화와 도구의 advisory lock으로 막는다.
- hook은 동기화마다 돈다(self-heal 포함). 마이그레이션 도구가 적용 이력을 기록하는지(멱등성)를 게이트에서 확인한다. 부분 동기화에서는 hook이 돌지 않으므로 deployer는 부분 동기화를 쓰지 않는다.
- 같은 커밋에서 실패한 동기화를 Argo가 자동으로 다시 시도하지 않는다. 실패는 deployer가 감지해 LKG를 커밋한다.
- Canary·Blue-Green을 쓰면 구·신 버전이 새 스키마 위에서 동시에 돌므로 확장→이전→축소가 필수다.

## 6. 시험 앱

| 앱 | DB 사용 방식 | 쓰는 시나리오 |
|---|---|---|
| inference-atlas (로컬 사본) | JSON으로 **빌드할 때** SQLite를 만들고, 런타임에는 읽기 전용(`immutable=1`) | M0 |
| memo-sqlite (새로 만들 최소 앱, "대충 만든 앱" 역할) | 런타임에 SQLite에 쓰기, `migrations/*.sql` | M1–M6 |

## 7. 시나리오

| # | 상황 | 사용자 입력 | 기대하는 플랫폼 동작 | 통과 기준 (결정론 확인) |
|---|---|---|---|---|
| M0 | 읽기 전용 SQLite 데이터셋 (atlas) | 폴더만 | Postgres로 옮기지 **않는다**. 빌드 단계에서 DB를 만들어 이미지에 읽기 전용으로 넣고, 보고서에 이유를 쓴다 | 이미지 안 DB 해시가 빌드 입력과 같음, API 200, `resources`에 postgres 없음 |
| M1 | 쓰기 SQLite 앱 → Postgres (킥오프 예시) | 폴더 + "운영 수준으로 올려줘" 또는 변경 요청 "DB를 Postgres로" | `resources.postgres` 추가, 역할 셋 생성, SQL 방언 변환, SQLite 데이터 1회 이전 Job, 앱이 `DATABASE_URL`을 읽도록 수정(D1에 따라 AI가 고치거나 `code_change_needed`로 안내) | 테이블별 행 수·체크섬 일치, 앱 쓰기·읽기 스모크 통과, 런타임 역할로 DDL 시도 시 거부 |
| M2 | 컬럼 추가 (확장) | "메모에 태그 추가해줘" (코드 변경 동반 → 재업로드) | `ADD COLUMN ... NULL` 마이그레이션 Job(wave 1) → 앱 롤링 | 롤링 중 이전 버전 요청도 200, Job 성공, risk=restart |
| M3 | 컬럼 삭제·이름 변경 (축소) | "안 쓰는 컬럼 지워줘" | destructive → 서버 확인 창, 즉석 백업 먼저, 새 버전 정상 확인 뒤 실행 | 확인 없이는 적용 거부, 백업 ID가 evidence에 기록 |
| M4 | 잘못된 마이그레이션 | 기존 데이터와 충돌하는 `NOT NULL` 추가 | 게이트의 임시 Postgres에서 실패 → F9 → fixer가 기본값·backfill 추가로 고치거나 give_up | 운영 DB 변경 0, 원인이 사용자 보고에 들어감 |
| M5 | 마이그레이션 성공, 새 앱 실패 | (M2 뒤) 시작 직후 죽는 버전 | 앱만 LKG로 롤백, DB는 새 스키마 유지(확장 원칙 덕분에 이전 앱 동작), diagnoser 설명 | 롤백 뒤 URL 200, 스키마 버전 유지, "데이터는 되돌리지 않음" 명시 |
| M6 | 동시 변경 | 두 사용자가 같은 앱에 변경 요청 | 두 번째 `apply`는 `base_rev`가 낡았다며 거부 → 다시 `change` | 마이그레이션 Job이 동시에 두 번 돌지 않음 |
| M7 | 복구 드릴 | 운영자가 어제 시점으로 복구 | 백업에서 새 `Cluster`로 복구해 행 수 비교 | 복구 시간과 데이터 일치 기록 |

## 8. 결정 필요

- **D1 재검토.** 선택지는 셋이다.
  - (a) 배포 산출물만 수정
  - (b) DB 연결·설정 계층처럼 좁은 범위의 코드 수정을 허용하고, diff 표시 + 앱 테스트 + 마이그레이션 게이트 통과를 조건으로 반영
  - (c) 제한 없음

  권고는 (b)다. M1이 이 결정에 달려 있다.
- **memo-sqlite 테스트 앱 작성:** Flask 또는 FastAPI + sqlite3 + `migrations/001_init.sql` 정도
- **앱마다 `Cluster` 하나 vs 공유 `Cluster`에 앱별 DB:** 격리와 수명주기가 단순한 앱별 `Cluster`를 권고한다. 단일 노드 메모리가 모자라면 공유로 바꾼다.
