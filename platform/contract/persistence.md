# 상태 저장과 마이그레이션 규약

결정: 2026-10-01. 제품 control DB는 **SQLAlchemy Core 2.0.54 + Alembic 1.20.0**을 사용한다. 서버 드라이버는 **psycopg 3.3.3**이다. 설치 버전은 `platform/control/requirements.txt`가 정본이다. HTTP나 worker에 DDL을 넣지 않는다.

## 적용 경계와 인터페이스

`Console API → ControlState → SQLAlchemy Engine → SQLite 또는 PostgreSQL`을 따른다. API/worker가 SQL을 직접 실행하거나 DB 접속 문자열을 요청 payload에서 받지 않는다. `ControlState`의 동일 메서드가 tenant 격리, account slot, Allow, worker generation, 입력 lease, event cursor를 집행한다. DB별 업무 로직을 복제하지 않는다.

| 저장소 | 현재 지원 | 의미 |
|---|---|---|
| 제품 control DB | SQLite file / PostgreSQL + psycopg | 같은 schema revision과 같은 상태 계약. SQLite는 관리자 로컬 실행, PostgreSQL은 서버 배치에 사용 가능 |
| CI `run_state.py`의 실행별 checkpoint | SQLite 고정 | VM 실행 증거의 로컬 보관소. control DB를 바꿔도 자동 이관하지 않음 |
| 관리자 비용 import CLI | SQLite 고정 | 기존 비용 export/예약 원장. 제품 control DB와 아직 하나의 DB가 아님 |
| 사용자가 배포하는 앱의 DB | 앱의 기존 migration tool | 이 문서의 플랫폼 metadata migration과 별도. 앱 DB 탐지는 `ir.json.database`, 실제 migration 허가는 CD 계약을 따름 |

MySQL/SQL Server 등은 SQLAlchemy에 dialect가 있다는 이유로 제품 지원에 포함하지 않는다. 지원 추가에는 실제 backend 계약 테스트가 필요하다. SQLite↔PostgreSQL **schema 호환**과 기존 데이터 **엔진 간 이관**은 다르다. 자동 data copy/cutover는 구현하지 않았다.

## 스키마 변경

- 정본은 `control/migrations/versions/`의 Alembic revision이다. 런타임에 `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE`, `metadata.create_all()`을 실행하지 않는다.
- 최초 schema는 고정된 `0001_control`이다. 배포 후 기존 revision을 수정하지 않고 새 revision을 만든다. revision이 런타임 가변 metadata를 import하지 않는다.
- 공통 테이블·제약은 Alembic `op.create_table`과 SQLAlchemy 타입을 사용한다. DB 문법이 다른 append-only trigger만 `migrations/sql/{sqlite,postgresql}-events-append-only.sql`로 분리했다.
- DML은 named bind parameter가 있는 SQLAlchemy `text`를 사용한다. 값이나 접속 문자열을 SQL에 보간하지 않는다. 원문 SQL·URL·driver exception은 API에 노출하지 않는다.
- autogenerate 산출물은 검토 초안이다. rename, data migration, 제약 변경을 자동으로 옳다고 승인하지 않는다. 현재 revision은 수동 작성한 migration이다.
- SQLite의 ALTER 제한은 후속 revision에서 Alembic `batch_alter_table`로 처리한다. 필요한 데이터 복사·제약·index를 검토하고 실제 DB에서 검사한다.
- 변경은 가능하면 expand → 코드 전환 → backfill 검증 → 별도 contract revision 순서다. 삭제·타입 축소·tenant key 변경은 별도 검토와 복구 확인이 필요하다.

## 실행·동시성

스키마 upgrade는 서버 기동 전에 운영 명령으로 실행한다. 서버는 현재 revision을 검사하고 미적용/알 수 없는 revision이면 시작을 차단한다. 자동 stamp나 기존 unversioned 테이블 삭제는 하지 않는다.

```sh
python -m pip install -r platform/control/requirements.txt
python platform/control/database.py upgrade --database-file /PRIVATE/control/state.sqlite3
python platform/control/database.py check --database-file /PRIVATE/control/state.sqlite3
# PostgreSQL URL은 권한 0600인 파일에 둔다. stdout/브라우저/커밋에 넣지 않는다.
python platform/control/database.py upgrade --url-file /PRIVATE/control/database-url
python platform/control/database.py check --url-file /PRIVATE/control/database-url
```

SQLite 디렉터리는 0700, 파일은 0600이며 symlink를 거부한다. foreign key를 활성화하고 `synchronous=FULL`을 사용한다. PostgreSQL은 UTF-8 DB와 전용 control schema/database를 사용한다. 운영 환경에서는 migration 역할과 runtime DML 역할을 분리하고, private 네트워크/TLS 접속 정책을 관리자 구성으로 지정한다. 코드가 role 또는 인증서를 임의 생성하지 않는다.

SQLite는 `BEGIN IMMEDIATE`, PostgreSQL은 짧은 transaction 동안 `control_lock` 한 행의 `FOR UPDATE`를 사용한다. account capacity 확인과 dispatch, Allow 소비와 operation intent 기록이 한 transaction에 들어간다. 이 전역 직렬화는 작은 관리자 PoC의 명시적 처리량 한계다. 실제 lock 대기 측정이 문제일 때 account별 lock과 lock 순서를 도입한다. 외부 VM/모델 실행 중에는 DB lock을 잡지 않는다.

migration은 SQLite write transaction / PostgreSQL advisory transaction lock으로 동시에 실행되지 않게 한다. PostgreSQL sequence의 gap은 허용한다. cursor는 증가 순서 식별자이고 연속된 개수를 뜻하지 않는다. 작업 FIFO는 명시적 `queue_seq`로 저장하며 SQLite `rowid`에 의존하지 않는다.

## 실패·복구·증거

- schema DDL 실패 시 transaction을 rollback한다. revision head를 적용했다고 기록하지 않는다.
- 기존 unversioned DB를 발견하면 그대로 보존하고 중단한다. operator가 schema·row count·hash와 backup을 확인해 별도 이관 절차를 작성해야 한다.
- append-only event와 원문 diagnostic은 분리한다. 원문은 인증된 관리자 경로에서만 반환하며, 로그 쓰기 실패는 성공 상태를 만들 수 없다.
- `downgrade`는 작업·승인·감사 이력을 파괴할 수 있어 제공하지 않는다. 승인된 backup/restore와 forward repair를 사용한다. 자동 백업이 구현됐다는 뜻은 아니다.
- PostgreSQL trigger는 일반 UPDATE/DELETE를 막는다. DB owner/superuser에 대한 변조 방지 서명은 아니다.

## 검증과 출처

`test_state.py`는 같은 계약을 SQLite와 실제 PostgreSQL에 실행한다. `RAILSHOT_TEST_POSTGRES_URL`이 없으면 PostgreSQL 검사는 SKIP이며 통과로 세지 않는다. 병렬 submit/claim/Allow 소비, stale lease, 재시작·tenant 격리·로그 cursor를 포함한다. `test_database.py`는 새 DB upgrade, 반복 upgrade/data 보존, 미적용 revision 차단, 실패한 DDL rollback, 기존 unversioned 데이터 보존을 검사한다.

선정 근거는 SQLAlchemy의 [Engine/dialect](https://docs.sqlalchemy.org/en/20/core/engines.html), Alembic의 [versioned migration](https://alembic.sqlalchemy.org/en/latest/tutorial.html), [SQLite batch migration](https://alembic.sqlalchemy.org/en/latest/batch.html), [autogenerate의 검토 범위](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)다. Alembic은 이미 Python 실행 계층을 사용하는 이 코드베이스에서 DB별 업무 구현을 늘리지 않고 schema revision을 관리할 수 있다. 별도의 Java migration runtime을 추가하지 않는다.
