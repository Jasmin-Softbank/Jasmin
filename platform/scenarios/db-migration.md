# DB 선택과 마이그레이션

2026-10-01 기준. 이 문서는 **현재 구현**, **선택 권고**, **아직 구현하지 않은 작업**을 구분한다. 원칙은 입력 코드와 설정에서 확인한 기존 DB 엔진을 보존하는 것이다. Next.js, FastAPI, Spring이라는 이름만으로 PostgreSQL을 배정하거나 SQLite를 자동 변환하지 않는다.

## 1. 현재 구현 범위

| 경로 | 현재 하는 일 | 아직 보장하지 않는 일 |
| --- | --- | --- |
| `platform/poc/intake.py` | manifest, lockfile, 프레임워크, 실행 명령, 포트 등 앱 증거 수집 | DB 엔진·쓰기 여부·원본 데이터 위치·마이그레이션 이력의 자동 판별 |
| `platform/schemas/jasmin.schema.json` | `resources.postgres: small`, 서비스별 `migrate.command` | SQLite 영속 볼륨 모드, 관리형 DB 전용 계약, DB별 연결·권한 검증 |
| `platform/render/render.py` | CNPG PostgreSQL 17 `Cluster`와 DB 역할·Secret, 권한 설정 Job, migration Job 렌더 | DB 설치/실행 성공, 데이터 이관, 백업·복구 성공 |
| `platform/gate/gate.py` L3 | 일회성 Docker PostgreSQL 17에 기존 migration 명령과 앱 기동 시험 | 기존 운영 데이터 호환성, 실제 운영 연결, DML/DDL 권한 분리 검증 |
| [`platform/infra/database.py`](../infra/database.py) | 명시적 DB 증거를 입력받아 선택안과 차단 이유 출력 | 파일 내용의 진위 검증, DB 연결, 리소스 생성, migration 실행 또는 배포 승인 |

현재 CNPG 렌더는 1개 인스턴스·1Gi 저장소이며 기본 StorageClass는 `local-path`다. 최신 AWS/GCP/Azure bootstrap은 `/var/lib/rancher`의 별도 보존 디스크 mount를 요구하지만 DB 전용 볼륨은 아니며, `local-path`의 노드 결합도 남는다. 디스크 보존이 다중 노드 HA나 자동 장애 복구를 의미하지 않는다. `Cluster.bootstrap.initdb`가 앱 DB와 owner를 만들며, 별도 `Database` 리소스를 생성하는 구현은 없다. PostgreSQL 17 이미지는 현재 태그 참조다.

렌더 순서는 Secret 생성 준비 → Cluster/역할 → 권한 설정 Job(wave 0) → migration Job(wave 1) → 앱(wave 2)이다. migration은 CI와 동일하게 `PORT`, 선언 env/Secret 참조, owner 연결인 `DATABASE_URL`·`MIGRATION_DATABASE_URL`을 받고, 앱은 제한된 rw `DATABASE_URL`만 받는다. 플랫폼이 관리하는 DB/PORT 필드는 env override할 수 없다. 기존 앱 도구가 다른 환경변수 이름을 요구하면 명시적 어댑터가 필요하다. DB 리소스의 현재 보호 설정은 `Prune=confirm,Delete=false`다. 이 설정은 백업을 대신하지 않는다.

L3 코드에서 임시 `postgres` 관리자 연결은 DB 준비에만 사용한다. migration은 `app_owner`, 앱은 DML/sequence 권한만 받는 `app_rw`로 나눴으며 기존 객체와 신규 객체의 권한을 설정한다. 이 변경은 로컬 코드 검사로 확인했고 실제 Docker DB 통합 시험은 아직 수행하지 않았다. 따라서 운영 rw 역할의 DDL 금지 검증을 완료했다고 표시하면 안 된다. `ScheduledBackup`, Barman 플러그인 구성, PITR, 복원 시험, SQLite→PostgreSQL 데이터 이관 runner, migration 도구 자동 감지는 현재 구현되어 있지 않다. CNPG 1.30의 `DatabaseRole`은 실제 지원 리소스이지만, 역할 선언만으로 기존 객체의 소유권·권한 이관까지 검증되는 것은 아니다. [CNPG 역할 관리](https://cloudnative-pg.io/docs/1.30/declarative_role_management/)

## 2. SoftBank 방향에서 확인되는 것

SoftBank는 Agentic STAR의 기존 시스템 연계, API/MCP를 통한 외부 도구 연결, 고객 환경에 맞춘 모델과 관리 기능을 설명한다. 따라서 RAILSHOT에서는 **기존 데이터와 연결 계약을 보존하고, 변경을 검토 가능한 단계로 제시하는 방식**을 설계 방향으로 삼는다. 이는 공식 설명을 바탕으로 한 제품 설계 판단이며, SoftBank가 특정 DB 엔진이나 아래의 배포 구성을 인증했다는 뜻은 아니다. 공개 자료에서 Agentic STAR의 RDS/PostgreSQL/MySQL 버전별 DB 지원표는 확인하지 못했다. [SoftBank 공식 발표](https://www.softbank.jp/corp/news/press/sbkk/2025/20251211_01/), [현재 서비스 소개](https://www.softbank.jp/business/service/ai/agentic-star/)

## 3. 사용자에게 제공할 선택지

여기서 ‘로컬 DB’는 앱 프로세스가 파일을 여는 SQLite를 뜻한다. 컨테이너 안에서 실행하는 PostgreSQL은 별도 서버형 RDB다. 컨테이너라는 배포 방식 자체는 데이터 영속성이나 백업을 보장하지 않는다.

| 선택 | 적합한 입력 증거와 조건 | 현재 지원 상태 |
| --- | --- | --- |
| DB 없음 | 사용자가 확인한 무상태 앱 또는 DB를 사용하지 않는 코드·설정 | DB 리소스를 만들지 않는 선택안 |
| 로컬 SQLite 유지 | 실제 `sqlite3.connect`/SQLite datasource, 단일 앱 파드, 낮은 쓰기 동시성 | 엔진 보존 권고. 쓰기용 영속 볼륨 렌더는 **미구현** |
| 컨테이너 PostgreSQL | 이미 PostgreSQL을 사용하거나, 다른 엔진에서 PostgreSQL로의 이관을 명시적으로 선택 | CNPG 렌더와 임시 DB 시험 제공. 실제 설치·백업·복원·데이터 이관은 별도 검증 필요 |
| 기존/관리형 RDB 연결 | 기존 PostgreSQL/MySQL 등 엔진, 승인된 연결 Secret 참조, 네트워크·TLS·권한 계약 | 엔진 보존 권고. 관리형 연결 전용 렌더와 RDS 등 생성은 **미구현** |

SQLite는 여러 reader와 하나의 writer를 지원한다. 네트워크 파일시스템을 공유 DB 파일처럼 쓰거나 많은 동시 writer를 처리해야 한다면 서버형 DB를 검토한다. RAILSHOT의 초기 SQLite 런타임 프로필은 보수적으로 **최대 파드 수 1**만 허용한다. 현재 replicas뿐 아니라 autoscaling의 maxReplicas가 2 이상이어도 차단한다. 단일 파드에서도 재배포 중 writer 중복을 막는 전략과 영속 볼륨·백업이 필요하다. 이미지 안의 불변 읽기 전용 데이터는 유지할 수 있지만, 읽기 전용이라는 증거 없이 예외를 적용하지 않는다. [SQLite 사용 지침](https://www.sqlite.org/whentouse.html)

기존 MySQL 앱은 Spring/Next.js/FastAPI로 작성됐다는 이유로 PostgreSQL로 바꾸지 않는다. 엔진 변경은 별도 migration 제안이다. 관리형 DB는 이 단계에서 URL이나 비밀번호 값을 받지 않고 `{name, key}` 형태의 승인된 Secret 참조만 받는다. 참조 형식 검증은 그 Secret의 존재·사용 권한·연결 가능성을 증명하지 않는다.

## 4. 앱 스택과 migration 도구

새 도구를 `latest`로 설치하기보다 저장소의 lockfile·wrapper·기존 CI 명령을 우선한다. 패키지 존재만으로 실제 DB 사용을 판정하지 않는다. 여러 엔진이 발견되면 DB별로 검토하며 하나로 합치지 않는다.

| 앱에서 확인할 증거 | 보존할 도구와 검토 명령 예시 | 검토할 점 |
| --- | --- | --- |
| TS/JS/Next: 실제 datasource/provider, Prisma 설정, migration 파일, lockfile 버전 | Prisma 2–7: `prisma migrate status` → `prisma migrate deploy`; Prisma 8: `prisma migration check` → `prisma db migrate --show` → `prisma db migrate` | major별 명령이 다르다. 기존 script가 우선이며 미확인 버전에 예시를 적용하지 않는다. Drizzle/Sequelize 등 기존 도구를 Prisma로 바꾸지 않는다. |
| Python/FastAPI: 실제 SQLAlchemy 연결 설정 또는 SQLite 파일 열기, `alembic.ini`, revision 파일 | Alembic: `alembic heads` → 검토된 `alembic upgrade head` | autogenerate는 후보 변경이다. rename 등 누락·오해 가능성이 있어 생성 결과를 검토한다. |
| Java/Spring: datasource/JDBC 설정, pom/Gradle lock·wrapper, `db/migration` | Flyway: `validate` → `migrate` | 기존 migration의 checksum과 적용 이력을 보존한다. 이미 적용된 파일을 수정해 통과시키지 않는다. |
| Java 또는 기타 스택: Liquibase changelog와 저장소의 고정 버전 | Liquibase: `validate` → `update-sql` → `update` | changelog 이력과 잠금 상태를 확인한다. SQL 미리보기를 실행 성공으로 표시하지 않는다. |
| 직접 작성한 SQL과 이력 테이블 | 기존 runner의 순서·checksum·실패 처리·잠금 확인 | SQL 파일만 있다는 이유로 범용 runner를 만들어 자동 실행하지 않는다. |

위 명령은 검토할 예시이며 이 문서나 추천 CLI가 실행하지 않는다. Prisma 현재 문서는 기존 `migrate deploy`와 새 `db migrate` 흐름의 차이를 명시한다. Alembic 자동 생성은 완전한 스키마 비교가 아니다. Flyway는 migration 검증과 이력을 제공하고, Liquibase는 changelog 잠금으로 동시 적용을 조정한다. [Prisma migration 적용](https://www.prisma.io/docs/orm/migrations/applying-a-migration), [Alembic autogenerate](https://alembic.sqlalchemy.org/en/latest/autogenerate.html), [Flyway validate](https://documentation.red-gate.com/flyway/reference/commands/validate), [Liquibase update](https://docs.liquibase.com/community/reference-guide-5-0/init-update-and-rollback-commands/update), [Liquibase 잠금](https://docs.liquibase.com/secure/user-guide-5-2/what-is-the-database-changelog-lock-table)

## 5. 스키마 변경과 데이터 이관을 분리한다

일반 스키마 변경은 저장소에 기록한 migration을 같은 엔진에 적용한다. 기존 앱과 새 앱이 함께 동작해야 하는 기간에는 컬럼/테이블 추가 → 호환 코드 배포 → backfill → 구버전 사용 종료 확인 → 제거 순서로 진행한다. 빈 임시 DB의 `upgrade` 통과만으로 기존 데이터 변환이나 이전 앱 호환성이 검증되지는 않는다.

적용 전에는 DB 버전과 현재 revision, 적용할 파일의 digest, 예상 DDL과 데이터 변경, 잠금·실행 시간 한도, 백업·복원 근거를 검토한다. migration 도구의 DB 잠금이나 명시적 DB advisory lock 등으로 동일 DB에 대한 실행을 직렬화한다. Argo 한 앱의 sync 순서만으로 다른 앱·운영자의 동시 migration을 막을 수 없다. Alembic 명령을 사용한다는 이유만으로 DB 전체 잠금이 생긴다고 가정하지 않는다.

트랜잭션으로 되돌릴 수 없는 DDL은 별도 실패·복구 절차가 필요하다. 앱 이미지 rollback과 DB `down`/복원은 다른 작업이다. 앱 rollback을 눌렀다고 운영 데이터를 자동으로 과거 상태로 덮어쓰면 안 된다. [Flyway migration과 트랜잭션](https://documentation.red-gate.com/flyway/flyway-concepts/migrations)

SQLite→PostgreSQL처럼 엔진이 바뀌는 경우에는 다음 순서를 별도 실행 계획으로 만든다.

1. **원본 확인:** 실제 SQLite 파일 위치, 쓰기 경로, 버전·확장, 테이블·키·인덱스, 데이터량, 서비스 중단 가능 시간을 기록한다. 타입, 날짜/시간, boolean, BLOB, collation, autoincrement/sequence 차이를 명시한다.
2. **일관된 백업:** SQLite Online Backup API 또는 `VACUUM INTO` 등 검토된 방식으로 스냅샷을 만들고 복원 시험을 남긴다. WAL 사용 중인 DB의 본체 파일만 복사한 것을 완전한 백업으로 간주하지 않는다. [SQLite 백업](https://www.sqlite.org/backup.html)
3. **대상 준비와 초기 복사:** 승인한 PostgreSQL 버전에서 검토된 schema migration을 적용한다. 기존 고정 버전 이관 도구나 검토된 프로그램으로 데이터를 읽고 파라미터화한 insert/COPY를 사용한다. SQLite `.dump`를 PostgreSQL에 그대로 입력하는 범용 변환은 제공하지 않는다. PostgreSQL의 서버 파일 `COPY`와 클라이언트 스트림 전송의 차이도 구분한다. [PostgreSQL 17 COPY](https://www.postgresql.org/docs/17/sql-copy.html)
4. **검증:** 테이블별 행 수, 키·외래키·NULL·업무 제약, 타입을 정규화한 주요 레코드/해시, sequence 다음 값, 앱 읽기·쓰기 동작을 확인한다. runtime 역할의 DDL 거부와 migration 역할의 허용도 별도로 시험한다.
5. **쓰기 중지와 전환:** 초기 복사 이후 변경분을 어떻게 반영할지 정하고, 쓰기 중지 → 최종 동기화/검증 → 승인된 연결 참조 변경 → smoke test 순서로 진행한다. 이 프로젝트에는 범용 CDC/자동 dual-write가 없다. 대상 DB, 원본 revision, 변경 digest와 검증 결과를 승인 대상에 묶는다.
6. **복구 창 유지:** 원본과 백업을 보존한다. 전환 후 새 DB에 쓰인 데이터를 고려한 복귀 조건을 미리 정한다. 연결 URL을 원본으로 되돌리는 것만으로 데이터 일관성이 복구되는 것은 아니다. 원본 폐기는 별도 승인한다.

같은 엔진에서 호스트만 옮겨도 백업/복원, 변경분 동기화, 전환 절차가 필요하다. PostgreSQL dump/restore는 PostgreSQL 간 이관 도구이며 SQLite 변환 도구가 아니다. [PostgreSQL SQL dump](https://www.postgresql.org/docs/17/backup-dump.html)

## 6. 실행 가능한 오프라인 선택 검사

입력은 수집한 증거의 **요약**이며, 현재 CLI는 파일 내용까지 검증하지 않는다. `stack`은 참고 문자열로만 받고 엔진 결정에 사용하지 않는다. 연결값 대신 Secret 참조를 사용하고, 입력 JSON에도 원문 비밀번호·토큰·URL을 넣지 않는다.

```json
{
  "engine": "sqlite",
  "evidence": [
    {"path": "testapps/memo-sqlite/app.py", "kind": "sqlite-open", "engine": "sqlite"}
  ],
  "access": "read-write",
  "max_replicas": 1,
  "requested_mode": "preserve"
}
```

```sh
python3 platform/infra/database.py /path/to/db-evidence.json
python3 -m unittest discover -s platform/infra -p 'test_database.py' -v
```

`PLAN`은 검토 가능한 선택안, `MIGRATION_REQUIRED`는 명시한 엔진 변경에 이관이 필요하다는 뜻이다. 두 경우 exit 0이지만 항상 `deployment_approved: false`다. `BLOCKED`, `BLOCKED_ENV`, `NEEDS_EVIDENCE`, 잘못된 입력은 exit 2다. SQLite의 `max_replicas: 2`는 차단되며, `requested_mode: container-postgres`를 명시하면 실행 없이 migration 계획만 제안한다. managed 선택에는 `connection_secret_ref: {"name": "app-db", "key": "DATABASE_URL"}`가 필요하다.

도구 증거를 별도로 제공할 때는 `migration: {"tool": "alembic", "version": "1.20.0", "evidence": ["alembic.ini"]}`처럼 저장소에서 확인한 버전을 넣는다. 이 버전은 입력 예시이며 설치 권고가 아니다. 버전이나 이력이 없으면 확인 필요 상태로 표시한다. migration 도구가 없는 앱을 migration 통과로 표시하지 않는다.

## 7. 검증 시나리오와 후속 연결

`testapps/memo-sqlite`는 이미 존재하는 Flask/SQLite 앱이며 직접 작성한 SQL migration을 사용한다. 새 샘플이나 Alembic 앱으로 잘못 설명하지 않는다. 다음 시나리오는 실행 결과를 별도 기록해야 하며, 문서에 있다는 이유로 통과 상태가 되지 않는다.

| 시나리오 | 필요한 증거 |
| --- | --- |
| M0 읽기 전용 SQLite 유지 | 실제 쓰기 부재, 동일 데이터와 앱 이미지, PostgreSQL 리소스 미생성 |
| M1 단일 writer SQLite | 영속 볼륨, 중복 writer 없는 교체, 재시작 후 데이터 보존, 백업 복원 |
| M2 SQLite scale-out 거부 | 현재/최대 replicas 2 이상에서 계획·렌더 모두 차단 |
| M3 기존 PostgreSQL migration | 빈 DB와 이전 revision+fixture DB에서 upgrade, 기존/새 앱 호환성 |
| M4 SQLite→PostgreSQL | 타입 매핑, 데이터 검증, sequence, 전환 및 복구 연습 |
| M5 migration 실패·중복 실행 | 실패 시 새 앱 배포 차단, DB 잠금/timeout, 부분 적용 상태의 복구 |
| M6 역할 및 기존 관리형 연결 | Secret 사용 권한, TLS/네트워크 연결, runtime DDL 거부, migration DDL 허용 |
| M7 백업·복원·폐기 | 실제 복원 결과, 원본 보존 정책, 데이터 폐기 별도 승인 |

현재 오프라인 단위 검사는 선택 정책을 검증한다. 실제 클러스터·운영 DB·데이터 migration·복원 시험 결과는 아니다. 다음 구현은 schema/render/gate 담당과 계약을 맞춘 뒤 진행한다.

- Intake에 DB별 증거·쓰기 여부·원본 데이터/도구 버전 계약을 추가한다. 여러 DB를 하나의 `resources.postgres`로 축약하지 않는다.
- SQLite 쓰기 모드에는 영속 볼륨과 교체 전략을 추가하고, 정적 replicas와 KEDA maxReplicas 모두 1로 제한한다.
- 관리형 모드는 엔진·runtime/migration Secret 참조·TLS·승인된 네트워크 목적지/포트를 검증한다. 현재 HTTPS 중심 egress만으로 PostgreSQL 5432 연결을 지원한다고 주장하지 않는다. DB 생성·비용 승인과 기존 연결 사용은 구분한다.
- 도구별 환경변수 어댑터와 migration 동시 실행 방지를 추가하고, 구현된 제한 역할 gate를 실제 DB에서 검증한다. renderer는 `-`를 포함하는 앱 유래 역할명에 identifier quoting을 적용한다.
- DB 리소스의 저장 공간·백업 비용·복원 가능성과 앱 replica 비용을 따로 계산한다. KEDA는 DB 용량이나 DB 노드를 자동 증설하는 기능이 아니다.

## 8. 노드·CSP에 종속되지 않는 DB 운영 — 제안, 미구현

목표는 특정 VM이나 CSP 디스크를 계속 붙잡는 것이 아니라, **다른 환경에서 데이터를 복원하고 검증한 뒤 쓰기 대상을 전환할 수 있는 것**이다.
현재 구현은 CNPG `instances: 1`, 기본 `local-path`, `bootstrap.initdb`뿐이다. 아래 백업·복구·복제·승격 경로와 RPO/RTO는 아직 구현·시험하지 않았다.
기존 M0–M7 및 스키마 migration 작업은 그대로 필요하며, 이 제안이 완료 근거를 대신하지 않는다.

### 우선순위: 두 번째 CSP에서 복원부터 검증

| 단계 | 제안하는 작업 | 완료 판정에 필요한 증거 |
| --- | --- | --- |
| 1. 백업 기반 독립성 | CNPG + Barman Cloud Plugin으로 물리 백업과 WAL을 별도 object store에 보존하고, 두 번째 CSP의 독립 클러스터에 복원 | 백업 ID·WAL 범위·목표 시점, 원본 없이 대상에서 복원한 결과, 데이터/앱 검증, 실측 RPO/RTO |
| 2. 복구 시간 단축 | CSP별 독립 Kubernetes/CNPG 클러스터 사이에 비동기 replica cluster를 유지 | 복제 지연·WAL 누락 감시, 네트워크 단절 시험, 승격·재합류 연습, 쓰기 primary가 항상 하나라는 확인 |
| 3. 승인된 전환 | 기존 primary의 쓰기를 차단한 근거를 확보한 뒤 대상 승격, 연결 endpoint 변경, pool 재연결, 읽기·쓰기 검증 | 원본 fencing → 승격 → 연결 전환 순서와 결과, 실패 시 중단 조건, 이전 primary의 재복제/재합류 절차 |

Barman Cloud Plugin은 물리 백업·WAL archive/restore·PITR를 제공한다. 적용 시 CNPG/플러그인 호환 버전을 고정하고 TLS·object-store 인증·보존 정책을 함께 준비한다. 백업이 원래 CSP에만 있거나 복원 자격을 원래 환경에서만 얻을 수 있다면 CSP 장애 시 독립 복구가 되지 않는다. [공식 플러그인 소개](https://cloudnative-pg.io/plugin-barman-cloud/docs/intro/)

2단계는 하나의 k3s를 WAN 너머로 늘이는 구성이 아니다. CSP별 제어면·스토리지를 분리하고 PostgreSQL 복제를 사용한다. CNPG의 DR용 distributed topology와 단순 읽기 전용 standalone replica cluster를 구분해 선택한다. replica를 추가했다고 CSP 간 자동 failover가 완성되는 것은 아니다. [CNPG replica clusters](https://cloudnative-pg.io/docs/1.27/replica_cluster/)

비동기 복제에는 미반영 쓰기 손실 가능성이 있으므로 RPO를 0으로 약속하지 않는다. RTO에는 복원/승격뿐 아니라 fencing, DNS/endpoint 전환, pool 재연결, 앱 검증 시간을 포함한다. 실제 장애·복구 연습에서 각각 측정한다. 네트워크 단절만으로 원본이 중지됐다고 판단하지 않으며, 원본 쓰기 차단을 확인할 수 없으면 승격을 차단한다.

전환 계획은 데이터 파일 외의 다음 계약도 함께 다뤄야 한다.

- PostgreSQL major 버전·확장 바이너리·collation과 migration revision의 호환성.
- owner/runtime/replication 역할, 권한과 승인된 Secret 참조; Kubernetes Secret/TLS 인증서가 WAL만으로 옮겨진다고 가정하지 않는다.
- 복제/백업 목적지 네트워크 허용, TLS 신뢰·hostname, 복원 측의 object-store 읽기 권한.
- 앱 endpoint·연결 pool의 재연결/기존 세션 종료, 읽기·쓰기 및 runtime DDL 거부 검증.

### 대안과 경계

- **VM·온프레 PostgreSQL:** Kubernetes 운영을 원하지 않으면 Patroni로 primary/standby와 DCS를 관리하고 pgBackRest로 백업·WAL·복원을 구성하는 대안을 검토한다. 두 DC의 비동기 standby 승격에서도 원본 fencing이 먼저이며, DCS quorum과 별도 백업 운영 책임이 생긴다. [Patroni multi-DC](https://patroni.readthedocs.io/en/latest/ha_multi_dc.html), [pgBackRest 안내](https://pgbackrest.org/user-guide.html)
- **Longhorn 또는 Rook/Ceph:** 볼륨 복제·복구 계층의 선택지다. 볼륨 HA/DR를 추가해도 PostgreSQL의 단일 writer, WAL/PITR, CSP 간 승격·데이터 일관성 정책이 자동으로 생기지는 않는다. DB 계층의 검증과 구분한다. [Longhorn 볼륨 복제](https://longhorn.io/docs/1.13.0/what-is-longhorn/), [Rook/Ceph 개요](https://rook.io/docs/rook/latest/Getting-Started/intro/)
- **분산 SQL:** 여러 지역의 쓰기 요구가 단일-primary PostgreSQL로 충족되지 않을 때만 별도 엔진 변경안으로 평가한다. SQL/트랜잭션 호환성, 지연·quorum, 데이터 이관과 운영 비용을 검증하기 전 기본안으로 바꾸지 않는다.

다음 구현 단위는 **백업 하나를 두 번째 CSP에 복원하는 M7 확장 시험**이다. 그 결과를 확보하기 전에 다중 CSP DB HA나 자동 승격을 지원한다고 표시하지 않는다.
