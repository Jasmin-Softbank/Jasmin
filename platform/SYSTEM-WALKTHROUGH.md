# 시스템을 학습하고 전달하는 순서

2026-10-01 작업 중인 소스 기준. 이 문서는 완료 보고서가 아니다. 실제 실행 결과는 감사 receipt로 확인하며, 테스트 중 소스가 바뀌면 해당 실행의 hash와 비교한다.

첫 질문은 “폴더를 올린 사람이 어떤 근거로 배포 성공을 믿을 수 있는가?”다. 디렉터리를 순서대로 외우기보다 요청 한 건의 입력, 저장 기록, 읽는 주체, 결정, 실패 시 복구를 추적한다.

## 1. 책임 지도: 5분

| 경계 | 하는 일 | 하지 않는 일 | 시작점 |
|---|---|---|---|
| 브라우저 | 폴더 선택, 작업 요청, 제어권/승인 입력, 상태와 로그 표시 | 자체 타이머로 성공 판정 | [app.js](../console/app.js) |
| 제품 API와 control DB | 인증, 작업/계정 슬롯, 승인, 임대, generation, 영속 이벤트 | 업로드한 코드에 클라우드 자격 전달 | [server.py](control/server.py), [state.py](control/state.py) |
| 개인 작업 VM | 업로드 사본, 결정적 CI, 수동 terminal, 실행 결과 기록 | 배포 자격이나 SDK 인증 보관 | [worker.py](control/worker.py) |
| 콘솔 메신저 | control DB의 이벤트와 관측 context를 SDK에 전달하고 실제 답변 저장 | 대화 응답으로 CI 실행·Allow 승인·배포를 대신함 | [chat.py](control/chat.py), [SSM transport](control/ssm_chat.py) |
| 에이전트 실행기 | 제한된 입력으로 packaging/소스 수정안을 생성하고 허용 경로 적용 | 자기 응답으로 검사 성공 판정 | [run_agent.py](runner/run_agent.py), [loop.py](loop/loop.py) |
| 인프라 실행기 | 등록한 CSP/온프레 대상의 자원 계획·변경·관측 | 앱 release를 인프라 생성 성공으로 대체 | [infra-interface.md](contract/infra-interface.md), [infra CLI](infra/README.md) |
| release/CD | 검증한 이미지 등록, GitOps 변경, Argo 상태와 서비스 도달 확인 | CI 통과만으로 배포 성공 표시 | [CD 계약](../ci/README.md), [workflow template](../ci/railshot-deploy.yml) |

**현재 배치와 목표 배치를 나눈다.** 이번 브라우저 통합 검증은 노트북의 loopback API → IAP SSH → GCP CI VM이다. 이 API를 아직 클라우드 상주 control 서비스로 배포한 것이 아니다. 따라서 현재 구성을 “맥북을 닫아도 전체 제품 동작”으로 설명하면 안 된다. 제품 control, 사용자 작업 VM, 앱 k3s는 역할과 자원 수명을 분리하는 목표 구조다.

## 2. 버튼 하나를 끝까지 추적: 10분

1. `app.js`의 업로드 handler는 파일을 `/api/upload`로 보낸다. 서버가 경로·비밀 패턴·크기를 검사한 뒤 비공개 manifest와 hash를 저장한다.
2. `CI 실행`은 `/api/jobs`에 종류와 idempotency key를 보낸다. `ControlState.submit`이 tenant/workspace, 제어권, 상태, 중복 요청을 검사한다.
3. 서버 작업자가 DB에서 claim을 얻는다. job ID와 generation을 붙인 요청을 관리자 등록 transport로 보낸다. 사용자 payload가 임의 VM이나 CLI 경로를 선택하지 않는다.
4. VM `worker.execute`는 같은 upload identity의 현재 작업 사본을 준비한다. `intake.py`와 `loop.py`가 실제 gate를 실행한다.
5. JSONL의 log/stage/result가 서버로 돌아온다. 서버는 identity·generation·종료 코드·결과를 검사하고 영속 이벤트와 job 상태를 저장한다.
6. SSE는 그 저장된 이벤트를 전달한다. 브라우저는 실제 결과를 표시하며 재접속 시 cursor와 snapshot으로 복구한다.

실습에서는 화면의 작업과 서버의 job ID, VM의 receipt 경로, verdict를 맞춰 본다. API가 202를 반환한 것은 접수이지 실행 성공이 아니다.

콘솔 대화는 별도의 제한된 queue를 사용한다. `POST /api/chat` → append-only queued/started/result 이벤트 → 고정 SSM transport → AWS 제어 VM의 Codex SDK → 실제 응답 artifact → SSE 순서다. 새 prepare/CI 완료도 같은 queue에 작업 ID로 한 번만 설명을 요청한다. 사용자 질문 없이 만든 설명 요청을 사용자 말풍선으로 표시하지 않는다. 이 메신저는 현재 관측 설명 전용이며 자동 수정 실행기와 권한이 다르다. 대화는 최근 기록을 명시적으로 전달하고, 원격 호출 결과가 불확실하면 자동 재호출하지 않는다. SSM은 Run Command 입력을 보관하므로 승인된 관측 요약만 전달하고 인증 정보는 제어 VM에 남긴다.

## 3. CI 판정과 모델 개입: 10분

실행 순서의 정본은 [execution.py](execution.py)의 `GATE_ORDER`다.

| 순서 | 검사 | 다음 단계가 읽는 근거 |
|---|---|---|
| L0 | 변경 경로·보호 파일·수정 범위 | diff와 경로 정책 |
| L1 | spec/schema·Dockerfile·실행 계약 | 검증한 spec와 구성 진단 |
| Q | manifest/lock/빌드 도구 기반 lint·type·unit | 실제 명령 결과와 테스트 report |
| L2 | 제한된 build context와 builder로 이미지 생성 | 해당 실행의 이미지 identity |
| L4 | 이미지 archive의 취약점·secret 검사 | 검사 보고서와 이미지 identity 결합 |
| L3 | 격리 환경에서 실제 기동·probe·해당 migration | 실행 결과와 health 증거 |

스캐너가 시작하지 못하거나 OOM으로 종료하면 취약점 발견으로 바꾸지 않는다. 테스트가 없거나 실행되지 않았으면 통과로 세지 않는다. Docker image ID와 scanner config digest처럼 생산자가 다른 필드의 의미도 확인한다.

`loop.py`가 실패 분류와 수정 권한을 보고 SDK를 호출한다. 원래 코드가 통과하면 호출은 0회다. source 수정은 명시된 범위와 적격 Q 실패에 한정되며, 인증/네트워크/설정 누락을 소스 수정으로 덮지 않는다. 수정 뒤에는 같은 gate를 다시 실행한다. 현재 콘솔 worker는 `--max-attempts 0`이며, **콘솔에서 SDK 자동 수정까지 연결된 상태는 아니다**. Codex/Claude의 공통 역할·출력 계약은 이미 별도 실행기에 있으므로 공급자별 CI를 복제하지 않는다.

## 4. 상태·수명·보안: 10분

| 구분 | 정본과 수명 | 알아야 할 복구 경계 |
|---|---|---|
| 제품 상태 | control DB의 workspace/job/Allow/lease/event | SQLite 또는 PostgreSQL. SQLAlchemy Core와 명시적 Alembic migration. 서버 기동에서 DDL을 하지 않음 |
| CI 실행 증거 | 실행별 SQLite checkpoint와 해시로 묶은 artifact | 완료 checkpoint를 resume. 불확실한 in-flight는 자동 재실행하지 않음 |
| 앱 데이터 | 사용자가 배포하는 DB/PVC/외부 RDB | 앱 migration과 backup/restore는 플랫폼 metadata migration과 별도 |
| VM | provider resource와 준비 관측 | `running`은 runner/desktop/cluster 준비 완료를 뜻하지 않음 |
| 파드 | Kubernetes 선언과 controller | 파드 재생성이 VM 또는 영속 데이터 삭제를 뜻하지 않음 |
| 수동 제어 | 만료되는 서버 lease | 버튼 색 변경이 아닌 서버의 상호 배제. 실행 중 CI와 동시 변경 금지 |

Terraform은 기반 자원과 provider state, Ansible은 VM 내부의 승인된 OS/도구/서비스 구성, Argo CD는 Git의 Kubernetes 선언 적용을 소유한다. 한 자원을 두 실행기가 동시에 소유하지 않는다. [인프라 계약](contract/infra-interface.md), [저장 규약](contract/persistence.md), [관측 규약](contract/observability.md)을 이 구분과 함께 읽는다.

IAP/SSM/worker의 outbound 연결과 앱 공개 HTTP 경로는 다른 경계다. CI VM에 공개 SSH/Argo 관리 포트를 열거나 release 권한을 넣는 것으로 연결 문제를 해결하지 않는다. 로컬 API의 단일 관리자 인증을 hosted 다중 사용자 인증으로 설명하지 않는다.

## 5. 전달 연습과 완료 판정: 5분

다른 사람에게 아래 다섯 문장을 코드와 증거를 짚으며 설명하면 전체를 이해한 것이다.

- “사용자는 이 입력만 주고, 플랫폼은 이 단계까지 대신한다.”
- “이 상태를 쓰는 주체는 이것이고, 화면은 이 기록을 읽는다.”
- “이 실패는 자동 수정 대상이고, 저 실패는 승인/설정/관측이 필요하다.”
- “재시작할 때 보존되는 것과 다시 실행하면 안 되는 것은 이것이다.”
- “현재 실제 통과한 구간은 여기까지이며, 미연결 구간은 이것이다.”

검증 장면은 정상 코드, 실제 unit 실패, 수동 수정 후 재검사, 화면 재접속, 승인 대상 변경으로 고른다. 계획된 기능을 완료 장면처럼 연출하지 않는다. 현재 Allow 소비→exact CI artifact release→GitOps→Argo 관측은 콘솔 경로에 미연결이다. 배포 버튼/URL을 가짜로 채우지 않는다.

팀 전달 자료는 이 책임 지도, 한 건의 실행 receipt, [GAP 원장](audits/2026-10-01-gap-ledger.md) 세 가지로 시작한다. 오래된 README의 상태 문장과 충돌하면 현재 코드와 해당 hash의 native receipt로 확인하고 문서를 정정한다.
