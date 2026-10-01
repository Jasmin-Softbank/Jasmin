# 플랫폼 GAP 감사 원장 — 2026-10-01

- 감사 ID: `GAP-20261001-182949`
- 시작: **2026-10-01 18:29:49 KST / 09:29:49 UTC**
- 수집 완료: **2026-10-01 18:50:28 KST / 09:50:28 UTC**.
- 원감사 상태(18:50): **38건 OPEN — P1 19건, P2 19건.** 이후 수정·검증 진행 상황은 아래 후속 기록을 따른다. 미구현 및 미검증도 포함하며 38건 모두를 재현된 취약점으로 세지 않는다.
- 기준: `feature/poc-cloud-jihwan`, base `ba414c19f982c778a2b15305e74d47f87583fc14`의 **dirty working tree + untracked 구현**. HEAD만으로 재현할 수 없다.
- 범위: 이전 사용자 요청·현재 계약과 실제 생산자→산출물→소비자→판정 경로. 정상 경로만 처리하고 실패·복구·권한·증거 경계를 빠뜨린 구현도 결함으로 평가한다.
- 원감사 단계는 수집과 재현·기록만 수행했다. 후속 사용자 승인으로 코드 수정과 별도 GCP CI VM의 실제 검증을 진행했다. 원감사 증거는 덮어쓰지 않는다.
- 관리자 전용 증거 위치: `~/.local/state/railshot/audits/2026-10-01/gap-review-182949/`. `source-snapshot.json`에 파일 hash·branch·기준 commit·시각을 보존한다. 해당 hash는 서명이나 호스트 관리자에 대한 변조 방지 보장은 아니다.

## 원래 담당 범위 우선 정리 — 2026-10-01 21:10 KST

- 사용자 지시로 AWS/CI·Terraform·Argo bootstrap·LKG를 먼저 닫는다. 현재 콘솔 연결의 회귀도 해결하며, 기능 확장으로 완료 기준을 대체하지 않는다. CodeBuild·AWS 새 앱 스택·실제 CD·LKG는 미완료다.
- 16개 분리된 로컬 회귀 실행에서 총 421개 검사가 통과했다. control 123에는 SQLite와 실제 로컬 PostgreSQL을 포함한다. SDK 19개는 실제 고정 SDK 타입을 사용하되 모델 경계는 모의다. 최초 SDK 패키지 부재/잘못된 Azure discovery 경로의 실패를 보존하고 환경·경로를 바로잡아 재실행했다. 서로 다른 실행을 하나의 전체 클라우드 PASS로 취급하지 않는다.
- 명시적 Alembic `0002_job_event_index` 적용 전 SQLite backup을 보존했다. 기존 jobs 8개/events 773개를 유지한 채 단계별 관측 조회를 연결했다. 이후 사용자 실행은 이 시점의 수치에 포함하지 않는다.
- GCP 앱 노드의 원본 identity 복구 후 platform workload 17개/Pod 17개 Ready, stale Node 객체 제거를 관측했다. 물리 VM·디스크는 삭제하지 않았다. CI VM 재부팅은 별도 시험이다.
- **신규 GAP-039 (CLOSED, 발견 21:00 / 검증 21:08 KST):** CI network service는 부팅 시 오래된 검증 fingerprint를 지우지만 probe를 재수행할 boot service가 없었다. 보안 우회가 아니라 readiness 복구 누락이다. 동일 Ansible 경로에 verification service를 추가했다. 실제 OS 재부팅 후 21:08:03 서비스 자동 시작→21:08:34 검증 증거 재생성→21:08:59 worker network/builder/ci_ready 모두 true를 확인했다. `network-after-reboot-r5.json`과 `inspect-native-r5-after-reboot.jsonl`로 이 결함의 인수를 닫았다. 증거 없이 준비 완료로 승격하지 않는다.
- **신규 GAP-040 (OPEN, 21:03 KST):** 새 구조화 chat schema의 실제 SDK turn이 실패했다. `SDK_OUTCOME_UNKNOWN` 이벤트와 immutable result를 보존했고 자동 재호출을 막았다. 모델 공급자 원인 및 정확한 terminal failure 판정·복구 경로를 조사 중이다.
- CI 단계 시작/종료 journal·worker 실시간 전달, chat pagination·구조화 DRAFT 렌더는 구현·로컬 검사를 완료했으나 신규 원격 판본의 프론트 인수는 진행 중이다. 원감사의 38 OPEN 항목과 신규 2건을 수정 없는 전체 CLOSED로 바꾸지 않는다.

공개 Git 체크포인트에는 코드·계약·판정만 게시한다. 비밀값, Terraform state/계획, 계정 원문, 사용자 로그 원문, 로컬 DB는 포함하지 않는다. private 근거: `gap-remediation-190434/precommit-tests/reviewed-results.json`, `gap-review-182949/console-worker-deployment/`의 r5 source/hash/native 결과. 이후 인수 결과는 원본 실패를 덮어쓰지 않고 후속으로 기록한다.

## 추가 인수 — 2026-10-01 20:31 KST 기준

- **native CI:** 10개 정상 스택의 full gate 10/10 PASS. negative 55/55 MATCH, 적용하지 않는 35개는 N/A이며 통과 수에 포함하지 않는다. Yarn/requirements fixture 권한 실패의 수정 재실행도 완료했다. 원 실패 기록은 유지한다.
- **브라우저 실제 경로:** Chrome 네이티브 폴더 선택으로 npm-js 10개 파일 업로드 → GCP prepare → full CI PASS(45.8초, L0/L1/Q/L2/L4/L3) → 수동 제어 획득 → VM 명령 출력 `RAILSHOT_MANUAL_CONTROL_OK` → 봇에게 반환을 검증했다. 클릭 전후 composer/header/window scroll 위치가 유지됐다. 고정된 성공 예시로 판정하지 않았다.
- **콘솔 SDK 대화:** AWS 제어 VM의 Codex SDK 0.159.3 / gpt-6.1-sol 실제 응답을 Chrome에서 확인했다. event와 결과 hash에 session/thread/turn identity가 남는다. 응답은 CI PASS와 배포 미확인을 구분했다. 자동 완료 설명·새로고침 지속성 검증은 진행 중이며, 콘솔 CI 자동 수정 연결과는 별개다.
- **신규 재시작 결함:** 앱 VM 한 개가 FQDN/shortname 두 Node로 등록돼 같은 systemUUID를 가진 상태를 확인했다. node-name 고정이 없으며 부팅 시 hostname 확정 순서가 유력 원인이다. 기존 FQDN identity를 명시하고 재시작 인수를 진행한다. 당시 PV/PVC와 사용자 앱 Pod는 0개였으며, retained disk는 삭제하지 않는다.
- **완료하지 않은 경계:** 제품 Allow→registry→GitOps→Argo→외부 HTTPS, 자동 SDK 수정, hosted 인증/상주 API, allocator, 전체 DB/복구/용량 확장. Artifact Registry와 앱 VM scoped reader 연결은 별도 변경 근거와 결과를 남긴다.

증거: 후속 private 디렉터리의 `ci-native/native-evidence-r1/evidence/final-matrix-r1.json`, `frontend-ci-control-r1.json`, `console-ci-control.png`; 원감사 디렉터리의 `chat-transport/chat-transport-ready.json`. 현재 API는 여전히 로컬이다. 서로 다른 revision의 회귀 수를 합산해 한 번의 전체 PASS로 표시하지 않는다.

## 후속 구현·검증 — 2026-10-01 20:04 KST 기준

아래는 원감사 이후의 기록이다. 상세 항목의 OPEN/행 번호는 원감사 시점의 관측이며, 수정 코드의 최신 위치와 다를 수 있다. **전체 38건이나 제품 전체 E2E를 닫았다는 뜻이 아니다.** local 회귀, native CI, 프런트 통합, 실제 CD를 구별한다.

| 항목 | 현재 조치와 검증 범위 | 아직 닫지 않은 경계 |
|---|---|---|
| 001–006, 008 | 공통 UID/command/security, 잘못된 migration/bucket admission 거부, 비어 있지 않은 render 출력 거부, probe 경로, SSM 실패 보존, child Application health 평가 구현·로컬 회귀 | 현재 소스의 실제 CD/클러스터 인수 |
| 007 | 실행되지 않는 backup 지원을 catalog에서 지원 완료로 표시하지 않음 | 실제 backup/restore/삭제·보존 자동화 |
| 009–015 | native Java checker/report 출처, 외부 이미지 정책, 물리 build context 제외, scanner failure 분류/메모리 경계, selected root 전달 구현. GCP npm-js full L0/L1/Q/L2/L4/L3 첫 PASS | 모든 스택/옵션의 native 인수; 신규 실패는 별도 시도 기록 |
| 016–023 | timeout/process group, observation 실패, 부분 패치, private root, auth binding, 필수 artifact, typed event, 시도/SDK/model 수 구분. runner19+loop31 로컬 PASS | 원격 SDK 취소·불확실한 실제 외부 호출의 reconciliation |
| 024–026 | loopback Console/API, 영속 kernel, SSE, 실제 GCP worker, 계정 슬롯·Allow·lease 구현. 실제 Chrome 폴더 업로드10파일→GCP prepare→UI PASS. HTTP/SQLite/PG 별도 회귀 | 콘솔 SDK 자동 수정·Allow 소비 후 release/CD, hosted 인증, provider VM allocator 연결 |
| 027–030, 032 | retained AWS disk/코드 commit binding/owner_ref/durable intent·budget hold 구현·로컬 회귀 | 실제 새 AWS 자원 수명/복구·비용원 수집 |
| 031, 033 | drain/lease kernel과 autoscaling 경계 유지 | VM timer→drain→checkpoint→stop의 실제 연결, replica→VM capacity 자동 조정 |
| 034 | 실제 GCP Q/BuildKit egress 및 내부 runtime 격리 검증; GCP/Azure 설정 보강 | 모든 CSP·CD/KEDA 전체 baseline에서 실제 검증 |
| 035 | intake의 실제 DB 탐지 연결. 별도로 control DB는 SQLAlchemy/Alembic SQLite·PostgreSQL 이식, SQLite20+실제 로컬 PG20 PASS | 앱 DB의 전체 배포/migration/복구; SQLite→PG 자동 데이터 이관은 미구현 |
| 036–037 | publish 이미지별 durable journal/reconcile, 공통 GATE_ORDER·execution/process·storage 사용 | 실제 registry 부분 실패/재개 인수, 남은 SDK version pin 중복 |
| 038 | 지원 스택별 good/negative native matrix 실행 중 | source SAST/coverage 등 남은 정책 및 전체 옵션 프런트→CD 인수 |

추가로 native 실행에서 Trivy cgroup OOM과 Docker29 OCI manifest/config digest 차이를 발견했다. scanner 전용 disk cache/한정 memory 및 archive 내부 manifest→config 해시 결합으로 수정했고 npm-js full PASS를 확인했다. 과거 BLOCKED/MISMATCH는 지우지 않는다. Yarn fixture 파일 권한 실패는 원인 검증 중이며 성공으로 세지 않는다.

현재 브라우저 서버는 노트북에 있고 CI만 GCP에서 실행한다. 클라우드 상주 제품 control 또는 맥북과 독립적인 전체 서비스로 설명하지 않는다. 사용자 작업 로그와 주기적 inspect 진단 분리·UI 준비 상태 깜박임 보완은 로컬22개 서버 회귀를 통과했으며 실제 프로세스 재시작/브라우저 재확인이 필요하다.

증거: 원감사 디렉터리의 `ci-fixes-tests-r2.json`, `runner-state-fixes-tests.json`, `infra-worker-fixes-validation.json`, `control-server-local-acceptance-r2.json`, `control-inspect-refresh-fix.json`, `console-worker-deployment/native-summary-r3.json`; 후속 디렉터리 `~/.local/state/railshot/audits/2026-10-01/gap-remediation-190434/`의 `control-database-native-r3.log`, `ci-native/first-pass-r6/demo-full/npm-js--good/result.json`. 서로 다른 시점의 테스트 수를 중복 합산하지 않는다.

## 판정과 우선순위

`DEFECT`는 계약과 실제 동작의 불일치, `NAIVE`는 알려진 실패 조건을 처리하지 않는 구현, `MISSING`은 요청된 경로 부재, `UNVERIFIED`는 인수 증거 부재, `DRIFT`는 문서·설정·구현 불일치, `DUPLICATION`은 독립적으로 바뀔 수 있는 중복 정책이다. 한 항목에 여러 판정이 가능하다. 계획에 미구현으로 명시된 항목을 허위 완료와 동일하게 취급하지 않는다.

P1은 다중 사용자 공개/실배포 전에 막아야 하는 신뢰·보안·데이터·성공 판정 결함, P2는 조건부 실패·운영 복구·유지보수 결함이다. `REPRODUCED_LOCAL`은 로컬 재현이고 클라우드 재현이 아니다. `SOURCE_CONFIRMED`는 호출 경로로 확인한 사실, `CONTRACT_ONLY`는 미구현 계약, `LIVE_NOT_RUN`은 실제 환경 미검증이다. 모든 OPEN 항목은 별도 수정과 인수 증거가 있어야 닫는다.

## 탐색과 증거 범위

| 범위 | 항목 | 판정 시 주의 |
|---|---|---|
| CI/CD 실행·렌더·DB·bootstrap | GAP-001–008 | 로컬 렌더/실패 주입과 소스 추론을 구별 |
| CI 품질 판정·공급망·실행 입력 | GAP-009–015 | Docker 운송 등 모의 경계 명시; 전체 release 미실행 |
| runner·state·resume·관측 | GAP-016–023 | 실제 임시 파일/SQLite/프로세스 + fault injection; 모델 호출 없음 |
| 제품 콘솔·VM·인프라·비용·용량 | GAP-024–033 | 소스 감사; 기존 명시적 미구현과 신규 결함을 구별 |
| 네트워크·DB 연결·publish·공통화·인수 | GAP-034–038 | 실서비스 및 전체 옵션 E2E 완료를 주장하지 않음 |

각 항목은 원인·소유 경계·인수 조건을 기준으로 묶었다. GAP-007의 DB 논리 백업/보존과 GAP-027의 AWS 물리 disk, GAP-017의 agent receipt와 이미 고친 gate receipt, GAP-030의 Terraform 실행 의도 기록과 GAP-036의 이미지별 publish 관측은 별도 실패 경계다. 한 수정이 여러 항목을 닫을 수 있지만 해당 항목별 인수 근거가 필요하다. 심각도는 현재 PoC에서 이미 피해가 발생했다는 뜻이 아니라 제품 통합/공개 전 우선순위다.

| ID | 우선순위 | GAP | 판정 | 확인 범위 |
|---|---|---|---|---|
| [GAP-001](#gap-001) | P1 | CI와 CD의 command·실행 UID·migration 파일시스템 계약이 다름 | DEFECT/NAIVE | 소스 대조 |
| [GAP-002](#gap-002) | P1 | PostgreSQL 선언 없는 migration 요청을 CD 렌더러가 조용히 누락 | DEFECT | 로컬 재현 |
| [GAP-003](#gap-003) | P1 | bucket 리소스는 스키마에 있지만 실행 산출물이 없음 | DEFECT/MISSING | 로컬 재현 |
| [GAP-004](#gap-004) | P2 | 재렌더 시 오래된 DB YAML이 남고 새 hash에서 빠짐 | DEFECT/NAIVE | 로컬 재현 |
| [GAP-005](#gap-005) | P2 | 공개 URL 검사가 선언된 경로 대신 항상 `/`를 검사 | DEFECT/NAIVE | 로컬 재현 |
| [GAP-006](#gap-006) | P2 | 설정된 SSM credential 조회 실패가 exit 0으로 소거됨 | DEFECT/NAIVE | 로컬 재현 |
| [GAP-007](#gap-007) | P1 | DB 백업·보존·삭제 계약이 실행 기능과 연결되지 않음 | MISSING/DRIFT | 소스 대조 |
| [GAP-008](#gap-008) | P2 | App-of-Apps wave 숫자만으로 operator 준비를 보장하지 못함 | NAIVE/UNVERIFIED | 소스·공식문서 추론 |
| [GAP-009](#gap-009) | P1 | Java report 출처와 실제 테스트 실행을 구분하지 못함 | DEFECT/NAIVE | 로컬·경계 모의 |
| [GAP-010](#gap-010) | P1 | 외부 COPY 이미지가 검사한 공급망 범위 밖에 있음 | DEFECT | 로컬·경계 모의 |
| [GAP-011](#gap-011) | P2 | .dockerignore substring 검사는 실제 build-context 제외를 증명하지 않음 | DEFECT/NAIVE | 로컬·경계 모의 |
| [GAP-012](#gap-012) | P1 | Trivy 시작 실패가 취약점 발견 및 fixer 대상으로 변환됨 | DEFECT | 로컬·경계 모의 |
| [GAP-013](#gap-013) | P2 | Java E2E의 실패 원인 확인이 unit 한 종류로 축약됨 | DEFECT/NAIVE | 로컬·경계 모의 |
| [GAP-014](#gap-014) | P1 | L2/L4의 로그 캡처에는 호스트 메모리 상한이 없음 | DEFECT/NAIVE | 로컬·경계 모의 |
| [GAP-015](#gap-015) | P2 | build root 선택은 준비 화면의 계획이며 실행 입력으로 이어지지 않음 | MISSING/NAIVE | 로컬·경계 모의 |
| [GAP-016](#gap-016) | P2 | timeout 후 실행 트리가 남을 수 있다 | DEFECT/NAIVE | 로컬·실패 주입 |
| [GAP-017](#gap-017) | P2 | 마지막 event 기록 오류가 기존 완료 receipt에 가려진다 | DEFECT | 로컬·실패 주입 |
| [GAP-018](#gap-018) | P2 | 중간 패치 쓰기 실패가 실제 변경 목록을 숨긴다 | DEFECT | 로컬·실패 주입 |
| [GAP-019](#gap-019) | P2 | private run root가 기존 디렉터리에 적용되지 않는다 | DEFECT/NAIVE | 로컬·실패 주입 |
| [GAP-020](#gap-020) | P2 | effective Codex 인증 경로가 resume 입력에 완전히 결합되지 않는다 | DEFECT | 로컬·실패 주입 |
| [GAP-021](#gap-021) | P2 | 없는 필수 artifact를 성공 checkpoint에서 조용히 제외한다 | DEFECT/NAIVE | 로컬·실패 주입 |
| [GAP-022](#gap-022) | P2 | 최종 실패 이벤트가 원인과 복구 정책을 갖지 않는다 | DEFECT | 로컬·실패 주입 |
| [GAP-023](#gap-023) | P2 | 호출 전 차단을 LLM 실행 1회로 센다 | DEFECT | 로컬·실패 주입 |
| [GAP-024](#gap-024) | P1 | 별도 UI 시안은 있으나 제품 콘솔과 실제 VM 연결은 없다 | MISSING | 소스 대조 |
| [GAP-025](#gap-025) | P1 | 개인 VM·실행 슬롯·구독 계정 배정 YAML이 집행되지 않는다 | MISSING | 소스 대조 |
| [GAP-026](#gap-026) | P1 | 공통 Terraform CLI는 제품 인프라 CRUD/Allow 서비스가 아니다 | MISSING | 소스 대조 |
| [GAP-027](#gap-027) | P1 | AWS 앱 노드의 데이터 수명이 compute/root disk에 묶여 있다 | DEFECT | 소스 대조 |
| [GAP-028](#gap-028) | P1 | AWS bootstrap은 승인한 코드 hash와 실제 실행 코드를 결합하지 못한다 | DEFECT | 소스 대조 |
| [GAP-029](#gap-029) | P2 | executor와 NodeDescriptor의 owner_ref가 서로 다른 정본을 가리킨다 | DEFECT/DRIFT | 소스 대조 |
| [GAP-030](#gap-030) | P1 | apply 시도 표시가 crash-durable journal이 아니다 | NAIVE | 소스 대조 |
| [GAP-031](#gap-031) | P1 | timer STOP과 작업 drain/복구/운영 종료는 아직 별개다 | MISSING | 소스 대조 |
| [GAP-032](#gap-032) | P1 | 비용 ledger는 수동 관측이며 실행 예산을 차단하지 않는다 | MISSING | 소스 대조 |
| [GAP-033](#gap-033) | P2 | 앱 replica 확장과 VM 용량 관리 사이 자동 조정은 없다 | MISSING | 소스 대조 |
| [GAP-034](#gap-034) | P1 | 네트워크 보안 프로파일이 gate·CSP 전체에 적용되지 않음 | MISSING/UNVERIFIED | 소스 대조·미검증 |
| [GAP-035](#gap-035) | P1 | DB 선택 도구가 실제 intake·schema·배포 경로에 연결되지 않음 | MISSING | 소스 대조·미검증 |
| [GAP-036](#gap-036) | P1 | 이미지 publish의 부분 부작용이 공통 관측·복구 계약 밖에 있음 | DEFECT/NAIVE | 로컬 경계 모의 재현 |
| [GAP-037](#gap-037) | P2 | 공통 정책이 아직 여러 실행기에서 독립 상수로 반복됨 | DUPLICATION/NAIVE | 소스 대조·미검증 |
| [GAP-038](#gap-038) | P2 | 필수 품질 gate와 전체 지원 옵션의 인수 증거가 아직 닫히지 않음 | MISSING/UNVERIFIED | 소스 대조·미검증 |

## 확인된 GAP

<a id="gap-001"></a>

### GAP-001 — CI와 CD의 command·실행 UID·migration 파일시스템 계약이 다름

- 우선순위/분류/상태: **P1 · DEFECT/NAIVE · OPEN · SOURCE_CONFIRMED**
- 확인 시각: 2026-10-01 18:33 KST. 이전 공통화 이후 남은 신규 확인.
- 근거: `platform/gate/gate.py:190`은 numeric USER >=10000을 허용하고 L3는 이미지 USER를 사용한다. `gate.py:380`의 migration과 `gate.py:394`의 runtime은 `docker run IMAGE <command>`다. `platform/render/render.py:52`는 UID/GID 65532를 강제하고 `render.py:191`, `render.py:235`는 Kubernetes `command`로 설정한다. migration Job은 read-only root지만 L3 migration은 그렇지 않다.
- 영향: ENTRYPOINT가 있는 이미지에서 CI는 인자를 덧붙이고 CD는 ENTRYPOINT를 대체한다. UID 10001의 전용 파일 권한이나 migration의 rootfs 쓰기에 의존하는 앱은 CI와 CD의 동작이 달라질 수 있다. 동일 image ID만으로 실행 계약이 같아지지 않는다.
- 최소 개선/인수: 승인된 공통 container execution spec에서 양쪽 명령·UID·mount/security 설정을 생성한다. ENTRYPOINT+CMD, UID 전용 파일, migration 쓰기 실패 fixture를 실제 Docker와 렌더된 Pod 양쪽에서 검증한다.
- 공식 의미: [Kubernetes command/args](https://kubernetes.io/docs/tasks/inject-data-application/define-command-argument-container/). 실제 클러스터 재현은 미실행이다.

<a id="gap-002"></a>

### GAP-002 — PostgreSQL 선언 없는 migration 요청을 CD 렌더러가 조용히 누락

- 우선순위/분류/상태: **P1 · DEFECT · OPEN · REPRODUCED_LOCAL**
- 확인: 2026-10-01T18:33:12.819971+09:00 / 09:33:12.819971Z.
- 근거: `platform/schemas/jasmin.schema.json:122`의 migrate는 postgres 조건이 없고, `platform/render/render.py:282`는 has_db일 때만 migration을 생성한다. npm-js fixture에 migrate만 넣어 실제 `l1()` 호출 결과 errors=[]; render는 `10-migrate.yaml`을 생성하지 않았다.
- 영향: 요청을 수락한 뒤 배포 단계가 명령을 버린다. 외부 DB migration 지원으로 해석해서는 안 된다. full gate/실배포 통과를 재현한 것은 아니다.
- 최소 개선/인수: 현재 지원하지 않는 조합은 admission에서 명시적으로 거부하거나 승인된 external DB 바인딩과 migration을 구현한다. 요청한 migration은 실행 계획에 반드시 존재해야 한다.
- 증거: 관리자 증거의 `render-reproduction.json`, `reproduce-render.py`.

<a id="gap-003"></a>

### GAP-003 — bucket 리소스는 스키마에 있지만 실행 산출물이 없음

- 우선순위/분류/상태: **P1 · DEFECT/MISSING · OPEN · REPRODUCED_LOCAL**
- 확인: 2026-10-01T18:33:12.819971+09:00 / 09:33:12.819971Z.
- 근거: `platform/schemas/jasmin.schema.json:66`은 resources.bucket을 허용한다. `platform/render/render.py:269` 이후는 이를 소비하지 않는다. bucket.public_read=false를 넣어 L1 errors=[]이며 bucket 없는 spec과 manifest_sha256이 같았다.
- 영향: 사용자가 요청한 클라우드 자원을 준비하지 않아도 지원 스펙처럼 수락한다. provision의 별도 관리자 Terraform 경로와도 연결되지 않았다.
- 최소 개선/인수: 구현 전에는 schema/admission에서 명시 거부한다. 구현 시 plan→승인→생성→연결정보→삭제/보존 계약과 실제 resource receipt를 검증한다.
- 증거: `render-reproduction.json`, `reproduce-render.py`.

<a id="gap-004"></a>

### GAP-004 — 재렌더 시 오래된 DB YAML이 남고 새 hash에서 빠짐

- 우선순위/분류/상태: **P2 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL**
- 확인: 2026-10-01T18:33:12.819971+09:00 / 09:33:12.819971Z.
- 근거: `platform/render/render.py:281`은 기존 출력 디렉터리를 허용하며 `render.py:320`은 현재 files만 덮어쓴다. postgres+migrate를 렌더한 뒤 같은 경로에 DB 없는 spec을 렌더하면 `00-db.yaml`, `05-grants.yaml`, `10-migrate.yaml`이 남고 meta.db=false 및 새 files/hash에는 빠진다. Application directory는 meta.json만 제외한다(`gitops-template/clusters/aws/platform/20-tenants.yaml:61`).
- 영향: meta/hash가 설명하는 산출물과 Argo가 소비할 YAML이 달라진다. 현재 fresh hosted release 경로에서 재현했다고 주장하지 않는다. CLI/재사용 경로의 결함이다.
- 최소 개선/인수: 비어 있지 않은 출력 경로를 거부하거나 소유권 확인된 renderer 산출물 집합을 원자적으로 교체한다. 옵션 추가·삭제 후 실제 소비 파일 집합과 manifest hash가 일치해야 한다.
- 증거: `render-reproduction.json`, `reproduce-render.py`.

<a id="gap-005"></a>

### GAP-005 — 공개 URL 검사가 선언된 경로 대신 항상 `/`를 검사

- 우선순위/분류/상태: **P2 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(렌더)/SOURCE_CONFIRMED(workflow)**
- 확인: 2026-10-01T18:36:53.647478+09:00 / 09:36:53.647478Z.
- 근거: `ci/railshot-deploy.yml:286`은 render.json의 host URL에 `/`를 붙인다. 스키마는 `/` route를 필수로 하지 않는다. 실제 npm-js fixture의 선언·HTTPRoute·PostSync는 `/health`인데 workflow 대상은 `/`였다(`platform/render/render.py:309`, `render.py:322`).
- 영향: 지원하는 non-root route 앱이 정상이어도 공개 probe는 404로 실패할 수 있다. 반대로 `/`의 오래된 2xx/3xx는 배포한 revision의 응답임을 입증하지 않는다. HTTP/클러스터 재현은 미실행이다.
- 최소 개선/인수: renderer가 승인된 external probe URL과 검증 조건을 명시하고 workflow가 이를 소비한다. non-root route와 stale revision 응답을 검사한다. live Pod imageID/observedGeneration 및 revision-bound HTTP 부재는 이전에도 명시된 잔여다.
- 증거: `route-probe.json`. 최초 host Python 시도는 jsonschema 부재로 미실행, 기존 check venv 재실행을 기록했다.

<a id="gap-006"></a>

### GAP-006 — 설정된 SSM credential 조회 실패가 exit 0으로 소거됨

- 우선순위/분류/상태: **P2 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(실패 주입)**
- 확인 시각: 2026-10-01 18:35 KST.
- 근거: `infra/ansible/node.yml:55`, `node.yml:119`의 `ssm get-parameter ... 2>/dev/null) || exit 0`. 해당 task는 parameter 변수가 정의됐을 때 실행한다. 공급자 명령만 `false`로 바꾼 동일 실패 분기는 두 경우 모두 exit 0, 출력 없음이었다.
- 영향: 권한 오류·연결 실패·존재하지 않는 parameter를 정상적인 미설정과 구분하지 못한다. 이후 private image/repo 접근이 깨져도 초기 원인이 남지 않는다. 실제 AWS 호출은 하지 않았다.
- 최소 개선/인수: 미설정은 명시적 SKIPPED, 설정된 credential의 조회 실패는 secret-safe BLOCKED로 반환한다. 접근 거부/없는 parameter/일시 네트워크 실패의 진단을 비밀 없이 검증한다.
- 증거: `bootstrap-failure.json`.

<a id="gap-007"></a>

### GAP-007 — DB 백업·보존·삭제 계약이 실행 기능과 연결되지 않음

- 우선순위/분류/상태: **P1 · MISSING/DRIFT · OPEN · SOURCE_CONFIRMED/CONTRACT_ONLY**
- 확인 시각: 2026-10-01 18:35 KST. 기존 미구현 재확인 + catalog 표현 불일치.
- 근거: `platform/contract/catalog.yaml:11`은 backups_days=7과 AWS S3 backup을 제공 범위처럼 기록한다. `platform/render/render.py:62`의 CNPG Cluster에는 backup/ObjectStore/ScheduledBackup 설정이 없다. `catalog.yaml:30`의 detach/retain 7일·승인·snapshot 계약도 실행 controller가 없다. 단일 app Application이 DB와 workload를 함께 소비한다. 계획 `platform/CONTROL-PLANE-PLAN.md:251`은 이를 미구현으로 정확히 표시한다.
- 영향: 보존 annotation만으로 backup/복원·namespace 삭제 방지·compute 교체 데이터 보존이 완성되지 않는다. catalog를 읽는 에이전트가 실제 제공 범위를 과대 해석할 수 있다.
- 최소 개선/인수: catalog capability를 실제 상태와 맞추고 workload/resource lifecycle을 분리한다. 데이터 표식→Pod 교체→workload 중지→namespace 삭제 거부→새 저장소 restore의 실제 증거를 요구한다. AWS 물리 disk 수명 문제는 인프라 항목에 별도 기록한다.

<a id="gap-008"></a>

### GAP-008 — App-of-Apps wave 숫자만으로 operator 준비를 보장하지 못함

- 우선순위/분류/상태: **P2 · NAIVE/UNVERIFIED · OPEN · SOURCE_CONFIRMED + 공식 동작에 따른 추론**
- 확인 시각: 2026-10-01 18:37 KST.
- 근거: `gitops-template/clusters/aws/platform/10-operators.yaml:6`의 operator Application은 wave 0, `20-tenants.yaml:40`은 wave 1이다. `infra/ansible/files/argocd.yaml.j2:20`에 Application health customization이 없고 `infra/ansible/node.yml:108`은 Argo Application CRD만 기다린다. [Argo 공식 health 문서](https://argo-cd.readthedocs.io/en/stable/operator-manual/health/#argocd-app)는 Application health 평가 제거와 App-of-Apps wave 사용 시 복구 필요를 명시한다.
- 영향: 선언 순서와 CNPG/ESO/KEDA controller·CRD 준비 완료를 동일하게 취급하면 최초 부트스트랩에서 하위 리소스 적용이 앞설 수 있다. 해당 pinned stack의 실제 cold-start 실패를 재현한 것은 아니다.
- 최소 개선/인수: 실제 설치 판본의 health 평가와 capability 준비를 확인하고 명시적으로 대기한다. 깨끗한 cluster에서 느린 operator 설치·누락 CRD를 주입해 준비 전 tenant sync를 막는지 검증한다.

<a id="gap-009"></a>

### GAP-009 — Java report 출처와 실제 테스트 실행을 구분하지 못함

- 우선순위/분류/상태: **P1 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(오프라인·일부 경계 모의 처리)**
- 확인 시각: 2026-10-01 18:36:36 KST; 독립 재확인 18:39:41 KST.
- 증거: 관리자 증거의 `ci-reproduce.json`, `parent-ci-recheck.json`.

**근거:** `platform/gate/quality.py:283`, `platform/gate/quality.py:334`, `platform/gate/quality.py:471`, `platform/gate/quality.py:558`.

- **Trigger:** 이름만 `EmptyTest.java`인 주석 파일, 형식에 맞는 wrapper checksum 속성, Checkstyle plugin 선언, 실제 Maven을 호출하지 않는 업로드 `mvnw`를 제공한다. 이 wrapper는 `verify` 때 `<testsuite tests="1" failures="0" errors="0" skipped="0"></testsuite>`를 stdout에 출력하고 0으로 종료한다.
- **Observed:** `java_plan()`은 이 조합을 승인했다. 플랫폼이 읽는 JUnit report 구간은 비어 있지만 `test_count()`는 전체 stdout에서 XML을 찾아 **1개**를 센다. 실제 생성된 plan을 임시 shell에서 실행하고 Docker transport만 바꾼 `run_quality()` 결과는 **`PASS`, tests=1**이었다. 실제 JDK·테스트 엔진·lint 호출은 0회다. 결과 키: `A_forged_java_report`.
- **Impact:** wrapper에 checksum 속성이 있다는 사실과 테스트 파일 이름이 있다는 사실이 실제 검사를 수행했다는 증거로 둔갑한다. 정상 fixture의 성공만으로는 발견되지 않는 거짓 양성이다. 신뢰할 수 없는 업로드 또는 잘못된 custom wrapper가 release의 Q 전제조건을 충족시킬 수 있다. 실제 full Docker gate/release를 재현한 주장은 아니다.
- **최소 개선:** Java도 지정한 report 구간 밖의 XML을 거부하고, 새로 생성된 report의 테스트 case 노드·count·failure/skip를 교차 검사한다. 실행 전 기존 report를 제거하고 실행별 report 경로를 쓴다. wrapper 분포 checksum과 wrapper 스크립트의 신뢰를 구별해, 검증된 wrapper 템플릿/바이너리 또는 플랫폼이 검증하여 설치한 배포판으로 실행한다. 사용자가 제공한 build/test 코드 자체가 임의 동작을 할 수 있다는 잔여 한계도 명시해야 한다.
- **필수 회귀:** report 구간 밖 XML, 테스트 case 없는 `tests=1`, stale report, fake wrapper, 빈 테스트 파일을 모두 거부하는 음성 fixture.

<a id="gap-010"></a>

### GAP-010 — 외부 COPY 이미지가 검사한 공급망 범위 밖에 있음

- 우선순위/분류/상태: **P1 · DEFECT · OPEN · REPRODUCED_LOCAL(오프라인·일부 경계 모의 처리)**
- 확인 시각: 2026-10-01 18:36:36 KST; 독립 재확인 18:39:41 KST.
- 증거: 관리자 증거의 `ci-reproduce.json`, `parent-ci-recheck.json`.

**근거:** `platform/gate/gate.py:140`, `platform/gate/gate.py:183`.

- **Trigger:** `FROM node:22-slim` 뒤에 `COPY --from=unapproved.invalid/unreviewed:latest /payload /payload`를 넣고 numeric USER와 exec-form CMD를 만족한다.
- **Observed:** `check_dockerfile(..., ['node:22-slim'])`가 **errors=[]**를 반환했다. 외부 레지스트리에는 접속하지 않았다. 결과 키: `B_external_copy`.
- **Impact:** FROM만 allowlist로 검사하므로 추가 외부 이미지에서 들어오는 바이너리는 catalog의 검토 범위를 우회한다. L4의 알려진 취약점 검사가 승인된 출처·정확한 공급망을 대신 증명하지 않는다. Dockerfile frontend(`syntax`) 등 다른 외부 입력도 별도 검토 대상이다.
- **최소 개선:** COPY/ADD의 `--from`을 같은 파일의 stage alias/index와 외부 이미지로 구분해 외부 이미지에도 같은 승인·pin 정책을 적용한다. 지원하지 않는 Dockerfile 문법은 허용으로 추정하지 말고 명시적으로 차단한다.
- **필수 회귀:** 허용된 stage alias는 통과하고, 외부 미승인 이미지와 변수를 통한 미확정 이미지 참조는 차단한다.

<a id="gap-011"></a>

### GAP-011 — .dockerignore substring 검사는 실제 build-context 제외를 증명하지 않음

- 우선순위/분류/상태: **P2 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(오프라인·일부 경계 모의 처리)**
- 확인 시각: 2026-10-01 18:36:36 KST; 독립 재확인 18:39:41 KST.
- 증거: 관리자 증거의 `ci-reproduce.json`, `parent-ci-recheck.json`.

**근거:** `platform/gate/gate.py:222`, `platform/gate/gate.py:328`, `platform/gate/bundle.py:38`.

- **Trigger:** `.dockerignore`를 `# .git\n# .env\n`로 두고 Dockerfile에서 `COPY . /app`을 사용한다. `.git/config`에 무해한 audit 값만 쓴 후 변경한다.
- **Observed:** L1 **errors=[]**, `.git/config` 변경 전후 `source_digest` **동일**. `.dockerignore`의 두 줄은 주석이므로 실제 제외 규칙이 아니다. 결과 키: `C_ignore_hash_gap`.
- **Impact:** L1은 `.git/.env`가 제외되었다고 오판한다. source hash에서 뺀 `.git`가 Docker context에는 들어갈 수 있어 “모든 build 입력을 묶은 source hash”라는 해석이 성립하지 않는다. bundle은 여전히 실제 image ID를 비교하므로 이 재현 자체가 다른 image를 publish했다는 증거는 아니다. intake는 `.env*`를 거부하지만 이 방어가 잘못된 Docker ignore 검사 자체를 정당화하지는 않는다. context별 또는 Dockerfile별 ignore 파일의 우선순위도 현재 검사에 반영되지 않는다.
- **최소 개선:** 유효한 context/해당 Dockerfile의 ignore 파일을 결정하고 Docker ignore 의미대로 검사하거나, 플랫폼이 `.git` 등을 원천 배제한 별도 build context를 만들어 그 정확한 입력을 hash한다. 주석·부정 규칙·우선순위 때문에 substring 검사만 강화하는 방식은 부족하다.
- **필수 회귀:** 주석만 있음, `!.git/**`, 하위 context, Dockerfile-specific ignore, hash에서 제외된 입력 변경 사례.

<a id="gap-012"></a>

### GAP-012 — Trivy 시작 실패가 취약점 발견 및 fixer 대상으로 변환됨

- 우선순위/분류/상태: **P1 · DEFECT · OPEN · REPRODUCED_LOCAL(오프라인·일부 경계 모의 처리)**
- 확인 시각: 2026-10-01 18:36:36 KST; 독립 재확인 18:39:41 KST.
- 증거: 관리자 증거의 `ci-reproduce.json`, `parent-ci-recheck.json`.

**근거:** `platform/gate/gate.py:450`, `platform/gate/gate.py:54`, `platform/loop/loop.py:25`, `platform/loop/loop.py:130`.

- **Trigger:** image size inspect는 성공하나 scanner의 `docker run`이 125와 `permission denied while trying to connect to daemon`으로 끝난다.
- **Observed:** 실제 검사 시작이 0회인데 `l4()`는 `web: trivy CRITICAL findings`를 스스로 붙였고 실제 classifier가 **F6**로 판정했다. 결과 키: `D_scan_infrastructure_classification`.
- **Impact:** 새 structured error 계층에서도 이 경로는 예외가 아니라 일반 `errors`로 전달되어 `GATE_CHECK_FAILED`가 된다. F6는 FIXABLE이므로 인프라 문제에 불필요한 packaging SDK 수정을 시도할 수 있다. 최대 시도 제한은 있어도 원인과 복구 행동이 틀렸다.
- **최소 개선:** scanner 실행 실패/DB 준비 실패와 유효한 취약점 finding을 구별한다. 성공적으로 파싱된 JSON scan report의 finding에만 F6를 부여하고 Docker 시작 실패는 환경 BLOCKED, timeout은 UNKNOWN으로 유지한다. stderr regex에만 의존하지 않는다.
- **필수 회귀:** Docker 125/126/127, scanner 설정 오류, DB 다운로드 실패, 유효한 empty report, 실제 finding report.

<a id="gap-013"></a>

### GAP-013 — Java E2E의 실패 원인 확인이 unit 한 종류로 축약됨

- 우선순위/분류/상태: **P2 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(오프라인·일부 경계 모의 처리)**
- 확인 시각: 2026-10-01 18:36:36 KST; 독립 재확인 18:39:41 KST.
- 증거: 관리자 증거의 `ci-reproduce.json`, `parent-ci-recheck.json`.

**근거:** `platform/gate/e2e.py:99`, `platform/gate/quality.py:405`.

- **Trigger:** `maven-spring`의 lint/type/unit 음성 사례 각각에 동일한 `status=FAIL, check=unit, source_repair_eligible=true`와 “unrelated unit assertion failed” 진단을 전달한다.
- **Observed:** 세 사례가 전부 **MATCH=true**다. 결과 키: `E_java_false_matches`.
- **Impact:** Maven/Gradle 통합 lifecycle을 실행한다는 합리적인 선택과 별개로, 깨진 lint/type fixture가 의도한 단계 때문에 실패했는지 E2E가 입증하지 못한다. 다른 unit 실패가 있어도 lint/type coverage로 집계될 수 있다. 현재 일반적인 정상/실패 fixture 통과 수는 이 제한을 포함한다.
- **최소 개선:** Java negative oracle에 checker/plugin/task/compile 진단의 구조적 근거를 추가한다. 실제 Java gate 결과를 unit으로 유지하더라도 E2E는 “기대한 Checkstyle/컴파일 원인”과 “다른 테스트 실패”를 구별해야 한다.
- **필수 회귀:** lint fixture에 unrelated test failure, type fixture에 Checkstyle failure를 주었을 때 MISMATCH.

<a id="gap-014"></a>

### GAP-014 — L2/L4의 로그 캡처에는 호스트 메모리 상한이 없음

- 우선순위/분류/상태: **P1 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(오프라인·일부 경계 모의 처리)**
- 확인 시각: 2026-10-01 18:36:36 KST; 독립 재확인 18:39:41 KST.
- 증거: 관리자 증거의 `ci-reproduce.json`, `parent-ci-recheck.json`.

**근거:** `platform/gate/gate.py:59`, `platform/gate/gate.py:331`, `platform/gate/gate.py:456`; 대비: `platform/gate/quality.py:538`.

- **Trigger:** Dockerfile RUN/build가 매우 많은 stdout/stderr를 생성한다. L4 또는 runtime logs도 같은 `sh()`를 거친다.
- **Observed:** `sh()`는 `subprocess.run(capture_output=True)`로 전량을 메모리에 축적하며 크기 제한 인수가 없다. 무해한 1 MiB 출력이 전량 `stdout`에 반환되는 것만 재현했다. 결과 키: `F_output_buffering`.
- **Impact:** 마지막 6,000/5,000자만 verdict에 쓰는 코드는 캡처 후 절단이므로 호스트 메모리를 보호하지 않는다. BuildKit/container 메모리 cap과 Q의 8 MiB 출력 파일 제한은 host Python의 L2/L4 버퍼에 적용되지 않는다. 작은 worker에서는 OOM·다른 job 장애가 가능하나 실제 OOM은 실행하지 않았다.
- **최소 개선:** 기존 Q 방식과 동일하게 bounded private log 파일 또는 bounded tail buffer로 읽고, 한도 초과를 structured BLOCKED/UNKNOWN으로 전환한다. timeout/로그 한도 이후 컨테이너·builder 작업 정리와 reconcile 상태를 함께 남긴다.
- **필수 회귀:** 작은 테스트 상한을 주고 상한 초과를 실제 child process로 재현해 메모리/파일 증가 제한과 다음 layer 차단을 확인한다.

<a id="gap-015"></a>

### GAP-015 — build root 선택은 준비 화면의 계획이며 실행 입력으로 이어지지 않음

- 우선순위/분류/상태: **P2 · MISSING/NAIVE · OPEN · REPRODUCED_LOCAL(오프라인·일부 경계 모의 처리)**
- 확인 시각: 2026-10-01 18:36:36 KST; 독립 재확인 18:39:41 KST.
- 증거: 관리자 증거의 `ci-reproduce.json`, `parent-ci-recheck.json`.

**분류: 실행 연결 미구현.** 단독 prepare 모듈은 스스로 read-only planner라고 명시하므로 planner 자체의 설치 실패라고 부르지 않는다.

**근거:** `platform/gate/prepare.py:114`, `platform/gate/prepare.py:183`, `platform/gate/quality.py:508`, `platform/gate/gate.py:674`.

- **Trigger:** 업로드에 `api`, `web` 두 독립 프로젝트가 있고 사용자가 `prepare_plan(selected_root='api')`로 하나를 선택한다.
- **Observed:** 선택 계획은 `api` 한 개다. 선택 없는 계획은 `status=READY`와 `selection_required=true`를 동시에 반환한다. 실제 gate Q의 `discover(ws)`는 `api`, `web` 두 개를 모두 고르며 gate CLI에는 selected-root 입력이 없다. 결과 키: `G_selected_root_not_gate_bound`.
- **Impact:** 사용자가 확인한 준비 계획과 검사·설치 대상이 다를 수 있다. 선택하지 않은 프로젝트의 missing lock/unsupported profile 때문에 선택 앱도 막히거나 불필요한 설치가 일어난다. 전체 monorepo를 검증한 것인지 서비스별 검증인지 결과 범위도 불명확해진다.
- **최소 개선:** 선택된 build graph를 source hash·spec의 service context와 묶은 trusted 실행 입력으로 전달하고 실제 Q 실행 대상과 일치시킨다. 선택이 필수라면 `READY` 대신 선택 대기 상태를 사용한다. 새 workflow engine은 필요 없다.
- **필수 회귀:** 두 독립 앱 중 하나가 의도적으로 깨져 있어도 선택한 앱만 검사하는 계약, 선택을 바꾸면 run binding이 달라지는 계약.

<a id="gap-016"></a>

### GAP-016 — timeout 후 실행 트리가 남을 수 있다

- 우선순위/분류/상태: **P2 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(상세 경계는 본문)**
- 확인 시각: 2026-10-01 18:38:27 KST까지 최초 재현; 독립 재확인 2026-10-01T18:39:42.129680+09:00.
- 증거: 관리자 증거의 `runner-state-repros.json`, `parent-runner-recheck/runner-state-repros.json`.

**위치:** `platform/loop/loop.py:30`, 특히 32–36행의 `subprocess.run(...timeout=1800)`.

**Trigger → observed → impact:** runner/gate 하위 프로그램이 자체 자식을 만든 뒤 1800초 제한에 걸림 → `subprocess.run`이 직접 실행한 프로세스를 종료하고 `STEP_TIMEOUT / UNKNOWN / after_reconcile`을 반환하지만 process group/session 관리가 없음 → 직접 자식의 후손이 계속 실행할 수 있다. loop의 자동 재호출 차단은 유지되지만, 작업 제한 시간이 실제 로컬 실행 트리 종료를 보장하지 않는다.

**재현:** `timeout_does_not_stop_child_tree`. timeout을 0.2초로만 바꿔 실제 로컬 부모/후손을 실행했다. 부모 종료 오류는 `STEP_TIMEOUT / UNKNOWN`이었고, 후손은 timeout 뒤 0.6초 시점에 marker를 썼다. stdout/stderr는 `/dev/null`로 분리하여 pipe 대기 효과와 구별했다.

**최소 개선:** 실행마다 전용 process group을 만들고 timeout 시 제한된 TERM→KILL 및 wait를 수행한다. 외부 모델/원격 실행 취소 여부는 별도 reconcile 대상으로 남겨 UNKNOWN을 유지한다. 로컬 종료 성공을 원격 호출 취소 성공으로 승격하면 안 된다.

**검증 수준/한계:** 로컬 process-tree 결함은 실제 프로세스로 확인. pinned Codex/Claude의 실제 orphan 여부, 외부 모델 지속 실행·비용은 이번 감사에서 확인하지 않았다. 제품의 원격 cancel API 미구현과 구분되는 현재 로컬 timeout 정리 결함이다.

<a id="gap-017"></a>

### GAP-017 — 마지막 event 기록 오류가 기존 완료 receipt에 가려진다

- 우선순위/분류/상태: **P2 · DEFECT · OPEN · REPRODUCED_LOCAL(상세 경계는 본문)**
- 확인 시각: 2026-10-01 18:38:27 KST까지 최초 재현; 독립 재확인 2026-10-01T18:39:41.064672+09:00.
- 증거: 관리자 증거의 `runner-state-repros.json`, `parent-runner-recheck/runner-state-repros.json`.

**위치:** `platform/runner/run_agent.py:483`, `platform/runner/run_agent.py:493`, `platform/loop/loop.py:74`, `platform/loop/loop.py:230`.

**Trigger → observed → impact:** provider 완료와 proposal 처리가 끝나 `<role>.json`을 저장한 뒤 마지막 `agent.completed` event 저장에 ENOSPC → runner stdout은 `OBSERVATION_WRITE_FAILED / BLOCKED / side_effect=completed / after_reconcile`, exit 1 → `loop.agent()`는 기존 disk receipt가 읽히면 stdout error를 보지 않아 `meta.status=completed`, `error=null`인 record를 반환한다. 이어지는 loop 분기는 일반 proposal rejection/FAIL로 축소하며 관측 저장 실패 원인과 복구 정책을 잃는다.

**재현:** `receipt_overrides_last_event_error`. mock provider + 실제 runner main/loop.agent + 마지막 events append에만 ENOSPC를 주입했다. 실제 반환: `returncode=1`, stdout error `OBSERVATION_WRITE_FAILED`, loop record error `null`, record SDK/status 둘 다 `completed`.

**최소 개선:** agent receipt에서도 child exit/stdout/receipt의 상태·error 일관성을 검증하고, child가 보고한 typed terminal observation error를 우선 보존한다. 필요하면 receipt에 후속 observation failure를 결합하되 SDK 완료와 platform 기록 실패를 별도 phase로 유지한다.

**검증 수준/한계:** 모델 없는 실제 함수 연결 재현. **PASS 승격을 확인한 것은 아니다.** exit 1이 후속 gate를 막는다. 이전에 고친 gate UNKNOWN→PASS 결함의 재발이라고 주장하지 않으며, 별도 agent receipt 경로의 원인 유실이다.

<a id="gap-018"></a>

### GAP-018 — 중간 패치 쓰기 실패가 실제 변경 목록을 숨긴다

- 우선순위/분류/상태: **P2 · DEFECT · OPEN · REPRODUCED_LOCAL(상세 경계는 본문)**
- 확인 시각: 2026-10-01 18:38:27 KST까지 최초 재현; 독립 재확인 2026-10-01T18:39:41.080763+09:00.
- 증거: 관리자 증거의 `runner-state-repros.json`, `parent-runner-recheck/runner-state-repros.json`.

**위치:** `platform/runner/run_agent.py:378`, `platform/runner/run_agent.py:451`, `platform/runner/run_agent.py:455`, `platform/runner/run_agent.py:478`.

**Trigger → observed → impact:** path/schema 검사를 통과한 두 파일 중 첫 파일 쓰기는 성공하고 두 번째 쓰기가 ENOSPC → 첫 파일 내용은 바뀌고 두 번째는 이전 내용인데 `apply_files()`가 끝까지 반환하지 않아 outer `written`은 `[]` → evidence의 변경 목록과 실제 workspace가 어긋난다. 해당 실패는 `after_reconcile / possible`로 멈추지만 운영자가 어떤 변경을 정리할지 receipt만으로 알 수 없다. 단일 파일도 직접 write이므로 실제 저장 실패 양상에 따라 부분 내용이 남을 수 있다(후자는 이번 실험 범위 아님).

**재현:** `partial_patch_missing_written_receipt`. 실제 결과 `Dockerfile=after`, `.dockerignore=before`, `written=[]`; error는 `INTERNAL_ERROR / phase=patch / FAIL / side_effect=possible`.

**최소 개선:** 모든 새 내용을 임시 파일에 먼저 써 검증·fsync하고, 원본 hash와 적용 진행 목록을 기록하며 개별 파일을 replace한다. 중간 적용 실패는 `applied_files`/미적용 목록과 원본 hash를 보존한다. 여러 파일 전체의 원자성을 보장하지 않으면서 성공처럼 표시하지 않는다. 최소한 이미 완료한 변경 목록을 예외 경로에서도 잃지 않게 한다.

**검증 수준/한계:** 실제 workspace 파일 쓰기 + 두 번째 쓰기만 fault injection. path 권한 우회나 후속 gate 실행은 관측하지 않았다. 기존 “모든 경로를 쓰기 전에 검증” 테스트는 이 I/O 실패 경계를 다루지 않는다.

<a id="gap-019"></a>

### GAP-019 — private run root가 기존 디렉터리에 적용되지 않는다

- 우선순위/분류/상태: **P2 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(상세 경계는 본문)**
- 확인 시각: 2026-10-01 18:38:27 KST까지 최초 재현; 독립 재확인 2026-10-01T18:39:41.094783+09:00.
- 증거: 관리자 증거의 `runner-state-repros.json`, `parent-runner-recheck/runner-state-repros.json`.

**위치:** `platform/loop/run_state.py:65`, `platform/loop/loop.py:70`, `platform/loop/loop.py:177`. standalone runner도 `platform/runner/run_agent.py:417`에서 mode 없이 mkdir한다. 규약은 `platform/contract/observability.md:51`.

**Trigger → observed → impact:** 운영자가 미리 만든 빈 0755 run 디렉터리를 넘기거나 standalone runner가 일반 umask 022에서 run 디렉터리를 만듦 → `mkdir(...exist_ok=True, mode=0700)`은 기존 디렉터리 권한을 바꾸지 않음 → task, lessons, intake inventory 및 복사된 소스처럼 기본 write/copy로 생성한 파일은 0644일 수 있다. 상위 경로가 다른 사용자에게 탐색 가능하면 private root 계약이 깨진다.

**재현:** `existing_run_directory_not_private`. 기존 root `0755`, synthetic task `0644`, SQLite `0600`을 확인. fixture의 상위 임시 디렉터리는 `0700`으로 유지하여 실제 데이터 노출은 만들지 않았다.

**최소 개선:** run 생성/재사용 시 소유자와 mode를 검증하고 안전하게 0700을 강제하거나 fail closed한다. standalone runner도 같은 작은 private-root helper를 사용한다. raw task/diagnostic 파일은 가능하면 생성 시 0600을 명시한다.

**검증 수준/한계:** 실제 mode 확인. 제3자 OS 계정 접근이나 실제 credential 노출은 검증하지 않았다. 현재 관리자 단일 사용자 PoC라는 범위와 별개로 문서화한 local private-storage 전제가 코드에서 강제되지 않는 결함이다.

<a id="gap-020"></a>

### GAP-020 — effective Codex 인증 경로가 resume 입력에 완전히 결합되지 않는다

- 우선순위/분류/상태: **P2 · DEFECT · OPEN · REPRODUCED_LOCAL(상세 경계는 본문)**
- 확인 시각: 2026-10-01 18:38:27 KST까지 최초 재현; 독립 재확인 2026-10-01T18:39:41.111208+09:00.
- 증거: 관리자 증거의 `runner-state-repros.json`, `parent-runner-recheck/runner-state-repros.json`.

**위치:** `platform/loop/loop.py:162`, `platform/runner/run_agent.py:305`, 특히 309행.

**Trigger → observed → impact:** `RAILSHOT_CODEX_HOME`이 없거나 빈 상태에서 지원된 fallback `CODEX_HOME`을 바꾸고 같은 run을 resume → binding은 `RAILSHOT_AUTH_MODE`, `RAILSHOT_CODEX_HOME`, `ANTHROPIC_BASE_URL`만 hash하므로 전체 binding이 동일 → 이후 아직 실행하지 않은 단계가 다른 관리자 인증 home을 사용할 수 있는데 “동일 auth route” 검사로 잡히지 않는다.

**재현:** `effective_auth_route_unbound`. 합성 절대 경로 `/offline-fixture/account-A`와 `/offline-fixture/account-B`를 사용해 실제 `loop.binding()` 전체가 동일함을 확인했다. home 디렉터리/자격 파일은 만들거나 읽지 않았다.

**최소 개선:** runner와 binding이 동일한 작은 effective-auth-config resolver를 공유하고, fallback 적용 후 mode와 정규화한 home 경로/승인된 endpoint 식별자를 결합한다. token 원문이나 auth.json을 읽거나 evidence에 넣을 필요는 없다. API-key 교체/계정 binding은 별도 명시 계약이 필요하며 이번 재현만으로 그 문제까지 입증하지 않는다.

**검증 수준/한계:** 순수 binding 함수 재현 + runner fallback 소스 대조. 실제 다른 계정 호출은 하지 않았다. workflow가 `RAILSHOT_CODEX_HOME`을 명시하는 경로에는 이 특정 fallback 재현이 적용되지 않는다. 문서에 이미 명시된 다중 사용자 인증 분리 미구현을 새 취약점으로 다시 세지 않는다.

<a id="gap-021"></a>

### GAP-021 — 없는 필수 artifact를 성공 checkpoint에서 조용히 제외한다

- 우선순위/분류/상태: **P2 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(상세 경계는 본문)**
- 확인 시각: 2026-10-01 18:38:27 KST까지 최초 재현; 독립 재확인 2026-10-01T18:39:41.090078+09:00.
- 증거: 관리자 증거의 `runner-state-repros.json`, `parent-runner-recheck/runner-state-repros.json`.

**위치:** `platform/loop/run_state.py:162`, `platform/loop/run_state.py:103`, `platform/loop/loop.py:225`.

**Trigger → observed → impact:** 성공 producer가 필수 receipt/sidecar를 남기지 않았거나 completion과 checkpoint 사이에 파일이 사라짐 → `artifacts` comprehension의 `if is_file()`이 누락 파일을 오류 없이 제거 → checkpoint에 `artifacts={}`가 저장되고 resume은 존재하는 목록만 검사하여 이를 완료 단계로 재사용한다. SDK 호출 전 실패에서 sidecar가 없는 경우와 성공 호출의 필수 증거 누락을 구분하지 않는다.

**재현:** `missing_required_artifacts_reused`. 실제 RunState에 agent 성공 fixture와 세 필수 파일명을 전달했지만 파일은 만들지 않았다. checkpoint 성공, `artifacts={}`, 재개 결과 `{'ok': True}`, function 재호출 없음 확인.

**최소 개선:** required/optional artifact를 caller가 명시하고 성공 agent의 receipt/events/session은 required로 검증한다. `gate failure.txt`처럼 정상 성공에서 없어도 되는 파일은 optional로 유지한다. “파일이 있으면 hash”가 “필수 증거가 존재한다”를 대신하지 않게 한다.

**검증 수준/한계:** 실제 SQLite 생성·종료·재개 재현. 자연 발생한 파일 유실이나 실제 모델 성공에서 누락된 사건을 관측한 것은 아니다. 현재 happy path의 runner는 보통 세 파일을 생성한다. 이는 누락 producer/저장 경계에 대한 failure-path 검증 GAP이다.

<a id="gap-022"></a>

### GAP-022 — 최종 실패 이벤트가 원인과 복구 정책을 갖지 않는다

- 우선순위/분류/상태: **P2 · DEFECT · OPEN · REPRODUCED_LOCAL(상세 경계는 본문)**
- 확인 시각: 2026-10-01 18:38:27 KST까지 최초 재현; 독립 재확인 2026-10-01T18:39:41.115062+09:00.
- 증거: 관리자 증거의 `runner-state-repros.json`, `parent-runner-recheck/runner-state-repros.json`.

**위치:** `platform/loop/run_state.py:185`, 특히 187–188행. common builder는 `platform/observability.py:131`에서 전달된 error를 정상 지원한다.

**Trigger → observed → impact:** 구조화된 error를 가진 정상적인 FAIL/BLOCKED 종료를 `complete(evidence)`로 저장 → final checkpoint에는 error가 있지만 `self.save('run.completed', outcome=..., passed=...)`에는 error가 전달되지 않음 → SQLite 최종 이벤트만 읽는 소비자는 실패 코드·side effect·retry policy·cause를 얻지 못한다. checkpoint를 추가로 읽어야 원인을 복원할 수 있고, 일부 정상 반환 실패는 앞선 `step.checkpointed`도 RUNNING이어서 이벤트 전체에 typed failure가 없을 수 있다.

**재현:** `terminal_event_loses_error`. final checkpoint error=`SDK_CONFIG_INVALID / BLOCKED / after_configuration / none`인 반면 `run.completed` event error=`null`, attributes=`{'passed': False}`였다.

**최소 개선:** 이미 검증된 `evidence.error`를 final event에 전달하고 outcome 일치 검사도 유지한다. 실패 증거를 별도 신규 프레임워크로 복제할 필요는 없다.

**검증 수준/한계:** 실제 SQLite/파일 비교. 최종 checkpoint 자체에서 원인이 지워진 것은 아니다. 제품 SSE/API는 아직 미구현이므로 사용자 화면 장애가 실제 발생했다고 표현하지 않는다. 현 event contract의 독립적 소비 가능성이 부족한 결함이다.

<a id="gap-023"></a>

### GAP-023 — 호출 전 차단을 LLM 실행 1회로 센다

- 우선순위/분류/상태: **P2 · DEFECT · OPEN · REPRODUCED_LOCAL(상세 경계는 본문)**
- 확인 시각: 2026-10-01 18:38:27 KST까지 최초 재현; 독립 재확인 2026-10-01T18:39:41.115641+09:00.
- 증거: 관리자 증거의 `runner-state-repros.json`, `parent-runner-recheck/runner-state-repros.json`.

**위치:** `platform/loop/loop.py:231`, `platform/loop/loop.py:325`. provider 미시작 상태는 `platform/runner/run_agent.py:52`에 존재한다.

**Trigger → observed → impact:** runner 설정/권한 오류로 `sdk_status=not_started` 상태에서 종료 → loop가 agent 프로세스에 들어갔다는 이유로 `agent_invoked=True`를 넣고 finish는 그 항목 개수를 `llm_calls`로 기록 → 모델 호출이 없는데 `llm_calls=1`, `cost_status=unknown`이 된다. 실행 시도 수와 실제 모델 호출 지표가 섞여 무호출 baseline/호출 예산 관측이 부정확하다.

**재현:** `no_sdk_call_counted_as_llm`. `SDK_CONFIG_INVALID`, `sdk_status=not_started`인 실제 finish 입력으로 `llm_calls=1`, `cost_usd=null`, `cost_status=unknown` 확인. 모델 호출 없음.

**최소 개선:** 현재 카운터는 `agent_attempts`로 명명하고, SDK 시작 전 차단은 확인된 SDK 호출 0으로 기록한다. SDK invocation과 내부 모델 turn/request 수는 구분하며, provider가 실제 request 수를 주지 않으면 LLM call 수를 임의 1로 만들지 않는다. 응답이 유실된 호출은 0이 아니라 unknown으로 유지한다.

**검증 수준/한계:** 실제 계산 함수 + 현재 caller의 `agent_invoked=True` 소스 대조. 실제 과금/금액 오류까지 확인한 것은 아니다. 비용 null을 0달러로 바꿔 기록하는 문제는 현재 소스에서 관측하지 않았다.

<a id="gap-024"></a>

### GAP-024 — 별도 UI 시안은 있으나 제품 콘솔과 실제 VM 연결은 없다

- 우선순위/분류/상태: **P1 · MISSING · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `KNOWN_NOT_IMPLEMENTED`.

**사용자 계약:** 펫 클릭 → 컴퓨터 메뉴 → 채팅/실제 VM 분할, repo·CI 터미널 기본 화면, 양방향 제어권, 같은 대화의 작업 상태. 계획의 약속은 `platform/CONTROL-PLANE-PLAN.md:12`, `:78`, `:88`, `:94`에 있다.

**현재 코드:** 저장소의 `console/` 파일은 `assets/nuvlet-pet.png`와 `assets/nuvlet-pet.prompt.txt` 두 개다. **별도 실제 UI 시안은 존재한다**: `~/.codex/visualizations/2026/09/30/01a0f464-ff06-7f40-a0eb-5c3fbf774068/railshot-ui-flow.html:89`가 “시안 · 실제 VM 미연결”을 표시한다. `:95`–`:104`는 widget의 로컬 화면 상태, `:113`은 450ms 타이머 뒤 `human` boolean을 바꾸는 제어권 시뮬레이션이다. 이 파일에는 제품 fetch/WebSocket/EventSource 연결이 없다. 부모 workspace의 HTML/TSX/package manifest도 가볍게 확인했으며 제품 앱은 추가 발견하지 못했다. 언급된 127.0.0.1:60435 서버는 이번 네트워크 금지 범위에서 접속하거나 전체 컴퓨터의 서버 부재를 판정하지 않았다. Jasmin 범위의 제품 서버/API client 부재와 계획 `:90`, `:92`, `:546`의 미구현 표시만 확정한다.

**영향/최소 조치:** 디자인 시안이나 펫 자산으로 업로드·진행 관측·실제 VM·제어권을 사용할 수 없다. 서버가 인증한 단일 workspace와 run을 연결한 최소 콘솔, WSS 화면 게이트웨이, 입력 lease/generation 검사를 먼저 연결한다. 바깥 Allow UI를 VM 화면과 분리한다.

**완료 인수:** 서로 다른 두 identity의 화면/상태 교차 접근이 거부되고, 펫부터 실제 터미널까지 연결된다. 입력 인계 ACK 전 입력 거부, lease 만료·재접속의 이전 generation 거부, 브라우저 종료 후 접수된 작업 지속을 확인한다. 픽셀·시안·mock 응답은 인수 증거가 아니다.

<a id="gap-025"></a>

### GAP-025 — 개인 VM·실행 슬롯·구독 계정 배정 YAML이 집행되지 않는다

- 우선순위/분류/상태: **P1 · MISSING · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `KNOWN_NOT_IMPLEMENTED`.

**사용자 계약:** 사용자 전용 작업 VM에서 CI runner 실행, 전체 활성 슬롯 1개, 관리자 계정 하나에 sticky 배정, 맥북 종료와 무관한 지속 실행. 계획 `platform/CONTROL-PLANE-PLAN.md:159`, `:160`, `:545`, `:682`.

**현재 코드:** `platform/runner/accounts.yaml:2`에 allocator 미구현, `:13`과 `:15`–`:22`에 동시 실행/queue/reset 정책은 설정값으로만 있다. runner는 `profiles.yaml`을 읽지만 accounts allocation/queue를 소비하는 코드가 없다. `infra/ansible/ci.yml:1`도 CI 설치가 runner registration을 하지 않는다고 명시한다. workspace Launch Template·create/start/stop worker·per-job runner 등록 서비스가 없다. 최근 loop의 flock은 **run 디렉터리별** 잠금(`platform/loop/run_state.py:69`)으로, 다른 run 사이 계정/VM 전체 슬롯을 직렬화하지 않는다.

**영향/최소 조치:** YAML의 `max_concurrent_runs: 1`만으로 중복 모델 호출·두 VM 할당·계정 사용량 대기를 보장할 수 없다. 제품 저장소에 workspace/account claim과 worker lease를 두고 하나의 할당 경로가 template·runner bootstrap·일시 자격 등록을 소유하도록 한다. 로컬 loop resume를 원격 durable dispatch로 표현하지 않는다.

**완료 인수:** 동시 두 요청과 worker 중단/재시작 시 동일 배정 복원, 두 번째 요청 대기, 외부 생성 응답 유실의 correlation readback, 계정 만료/한도 대기, VM에 관리자 모델 자격이 복사되지 않음을 확인한다.

<a id="gap-026"></a>

### GAP-026 — 공통 Terraform CLI는 제품 인프라 CRUD/Allow 서비스가 아니다

- 우선순위/분류/상태: **P1 · MISSING · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `KNOWN_NOT_IMPLEMENTED`.

**사용자 계약:** profile만 선택하고, 권한·비용·capability 검사 뒤 계획/Allow/operation, 조건부 변경, 관측 기반 readiness를 얻는다. `platform/contract/infra-interface.md:29`–`:59`, `:71`–`:77`.

**현재 코드:** `platform/infra/provision.py:18`은 AWS/GCP/Azure module allowlist, `:85`–`:97`은 trusted target와 opaque provider variables 입력, `:176`–`:180`은 `plan/apply` 두 CLI 명령뿐이다. `:82`는 product_allow not_implemented/readiness unverified를 명시한다. read/start/stop/delete API, capability/profile loader, tenant authentication, generation CAS, signed/consumed Allow, resource/operation 조회, readiness reconciliation은 없다. OpenStack Controller adapter 역시 이 CLI에 없다. `infra/README.md:25`도 CPU/메모리에서 provider size 자동 환산이 없다고 밝힌다.

**영향/최소 조치:** 공통 subprocess 재사용은 구현됐지만 novice 사용자의 입력을 안전한 인프라 작업으로 변환하는 상위 기능은 남아 있다. 새 추상 framework 대신 기존 제품 API/worker에 등록된 target/profile 로더, 생성·조회에 필요한 operation 정본, ownership/capability gate부터 연결한다. 지원되지 않은 온프레/전원/삭제 경로는 명시적으로 차단한다.

**완료 인수:** 같은 표준 profile 요청이 선택 target의 고정 변수로 변환되고, tenant 위조·만료/변경된 plan·stale generation·중복 operation은 provider 호출 전에 거부된다. create 완료와 guest/runner/cluster Ready가 다른 상태로 관측된다.

<a id="gap-027"></a>

### GAP-027 — AWS 앱 노드의 데이터 수명이 compute/root disk에 묶여 있다

- 우선순위/분류/상태: **P1 · DEFECT · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `KNOWN_DEFECT`.

**사용자 계약:** 앱·VM·데이터 수명 분리, compute 삭제/교체 뒤 데이터 보존. `platform/contract/infra-interface.md:109`.

**현재 코드:** `infra/terraform/aws/main.tf:109`–`:113`은 root gp3만 설정하며 별도 데이터 볼륨·명시적 `delete_on_termination=false`·mount prerequisite가 없다. 앱 노드는 k3s의 기본 데이터 위치를 root에 사용한다. 반면 control `main.tf:106`은 root 보존을 명시하고, GCP `main.tf:94`–`:103`은 별도 보호 disk, Azure `main.tf:116`–`:126`도 별도 보호 disk를 가진다. 기존 계획 `CONTROL-PLANE-PLAN.md:185`에 이미 등록된 결함이다.

**영향/최소 조치:** AWS 모듈을 데이터 보존 target으로 동일 취급하면 instance 교체/삭제에서 앱 DB·local-path PVC가 사라질 수 있다. 별도 encrypted EBS와 `/var/lib/rancher` mount 선행을 추가하고, 기존 노드는 승인된 backup/migration을 거친다. 단순 Terraform flag 변경이 기존 데이터 이전은 아니다.

**완료 인수:** retained volume ID·표식을 기록하고 compute만 교체한 뒤 같은 volume에서 k3s/앱 데이터를 읽는다. volume 삭제가 별도 승인 없이 계획되지 않고, wrong/missing mount에서는 빈 DB로 기동하지 않아야 한다. 이번 감사는 실제 교체/삭제를 하지 않았다.

<a id="gap-028"></a>

### GAP-028 — AWS bootstrap은 승인한 코드 hash와 실제 실행 코드를 결합하지 못한다

- 우선순위/분류/상태: **P1 · DEFECT · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `KNOWN_DEFECT`.

**사용자 계약:** image/commit/runtime을 고정하고 승인한 실행 profile/code가 그대로 적용된다. `platform/contract/infra-interface.md:71`, `:190`, `:193`.

**현재 코드:** AWS `infra/terraform/aws/main.tf:14`–`:16`은 `stable/current` AMI, `variables.tf:27`–`:30`은 branch 기본값이다. `cloud-init.yaml.tftpl:17`–`:19`는 해당 ref를 guest에서 pull한다. 공통 executor `provision.py:34`–`:43`은 모듈의 `.tf/.tftpl/lock` bytes를 hash하지만 원격 branch가 가리키는 playbook 내용은 hash하지 않는다. 따라서 **saved plan이 AMI ID를 고정하는 것과 별개로**, plan/apply/guest bootstrap 사이 branch가 이동하면 같은 승인 plan으로 다른 guest 코드가 실행될 수 있다. 기존 계획 `:188`의 pin GAP이 아직 남아 있다.

**영향/최소 조치:** AWS도 exact image와 40-hex bootstrap commit(또는 검토한 비밀 없는 bundle hash)을 강제한다. 공통 executor가 모든 cloud guest 코드까지 고정한다고 설명하지 않는다.

**완료 인수:** floating branch/image 입력 거부; 계획 hash에 bootstrap revision/bundle이 결합되고, guest의 실제 manifest readback과 일치한다. remote ref 변경에도 같은 plan의 실행 내용은 변하지 않아야 한다.

<a id="gap-029"></a>

### GAP-029 — executor와 NodeDescriptor의 owner_ref가 서로 다른 정본을 가리킨다

- 우선순위/분류/상태: **P2 · DEFECT/DRIFT · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `NEW_STATIC_FINDING`.

**사용자 계약:** `owner_ref`는 자원의 유일한 변경 권위이며 같은 자원에 대해 하나로 유지된다. `platform/contract/infra-interface.md:185`.

**현재 코드:** `platform/infra/provision.py:109`–`:114`는 `terraform:local:<정확한 private state path>`를 owner_ref로 강제한다. GCP `infra/terraform/gcp/outputs.tf:10`은 `terraform:infra/terraform/gcp:google_compute_instance.node`, Azure `infra/terraform/azure/outputs.tf:10`은 `terraform:azure:<target_id>`를 내보낸다. `platform/infra/specsheet.py:107`은 이를 그대로 출력한다. AWS `outputs.tf:1`–`:5`는 표준 descriptor 자체가 없다(이 부재는 README에 이미 명시).

**영향/최소 조치:** 향후 plan→descriptor→resource inventory 연결에서 같은 자원에 서로 다른 ownership key가 생긴다. 현재는 제품 inventory가 없어 실제 권한 우회가 발생했다고 주장하지 않는다. executor가 반환하는 표준 descriptor에 authoritative owner_ref를 주입/검증하고 provider-native resource address는 별도 필드로 분리한다. AWS output은 지원 대상으로 등록하기 전에 같은 최소 계약을 맞춘다.

**완료 인수:** 각 provider의 saved plan receipt·descriptor·specsheet·resource record에 같은 target/owner binding이 유지된다. 다른 state 경로/target의 descriptor 혼합을 거부하는 model-free 계약 검사와 기존 live state 이전의 no-change plan이 필요하다.

<a id="gap-030"></a>

### GAP-030 — apply 시도 표시가 crash-durable journal이 아니다

- 우선순위/분류/상태: **P1 · NAIVE · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `NEW_STATIC_FINDING`.

**사용자 계약:** 외부 실행의 결과가 불명확하면 무조건 재호출하지 않고 대조한다. `infra/README.md:44`, infra-interface `:75`–`:76`.

**현재 코드:** `platform/infra/provision.py:29`–`:31`의 `write_private()`는 `Path.write_bytes()` 후 chmod만 한다. `:163`–`:165`에서 `apply_attempted=true`를 그 함수로 기록한 뒤 Terraform apply를 시작하지만, file/directory fsync나 atomic replacement/transaction이 없다. 일반 예외 뒤 재호출 차단 테스트(`test_provision.py:98`–`:108`)는 있으나 host power failure 또는 저장 중 interruption의 durable-order 검사는 없다.

**영향/최소 조치:** 프로세스 정상 흐름에서는 방어하지만, 외부 변경이 시작된 뒤 호스트 장애로 이전 `apply_attempted=false` bytes가 살아남는 경우까지 차단한다고 보장할 수 없다. 중간 JSON 손상은 대체로 fail-closed지만 명확한 recovery receipt가 없다. executor 작업 journal을 최소 atomic-write+file/directory fsync로 만들거나 작은 SQLite transaction으로 durable attempted/unknown/observed를 관리한다. Terraform lock만으로 제품 operation의 결과를 확인했다고 간주하지 않는다.

**완료 인수:** subprocess dispatch 전에 attempted marker의 durable commit이 끝남을 검사한다. marker 저장 실패 시 apply 0회, apply 시작 후 forced process exit 시 다음 요청 자동 실행 0회, 관측 evidence 저장 실패 시 UNKNOWN/after_reconcile을 검증한다. host power-loss는 이번 감사에서 재현하지 않았으며 이 항목은 소스상 내구성 보장 결여다.

<a id="gap-031"></a>

### GAP-031 — timer STOP과 작업 drain/복구/운영 종료는 아직 별개다

- 우선순위/분류/상태: **P1 · MISSING · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `KNOWN_NOT_IMPLEMENTED`.

**사용자 계약:** 활성 CI/desktop을 유휴 판정으로 중단하지 않고, 종료 전 checkpoint/artifact/runner 정리 및 compute·volume·IP 인벤토리를 유지한다. `platform/contract/infra-interface.md:109`–`:111`.

**현재 코드:** control `infra/terraform/control/cloud-init.yaml.tftpl:15`–`:19`는 최초 72h 시각을 저장하고 `systemctl poweroff`를 실행한다. GCP `main.tf:11`–`:15`, `:114`–`:123`은 선택적 per-start native STOP이며 기본 null이다. 두 경로 모두 활성 작업/lease/drain을 조회하지 않는다. control timer가 최초 cutoff일 뿐이라는 제한은 template `:4`–`:5`에 이미 명시돼 있다. 계획 `CONTROL-PLANE-PLAN.md:660`에는 마지막 기록 기준 발표 전 control 종료 시각을 조정해야 한다는 미해결 항목도 있다; 현재 cloud timer를 재조회하지는 않았다.

**영향/최소 조치:** 비용 실험 cutoff를 안전한 제품 종료로 재사용할 수 없다. 정상 종료는 dispatch fence→active lease/job 조회→checkpoint/artifact→runner 정리→stop receipt로 구현하고, 강제 deadline은 별도 `interrupted` 사건으로 기록한다. 마감 후 disk/IP도 인벤토리·청구 범위에 남겨야 한다.

**완료 인수:** 활성 job·유효 desktop lease 중 정상 idle stop 거부, cutoff 시 중단 이유·작업 ID 보존, 재기동 후 외부 run 대조, compute OFF와 retained disk/IP를 별도 관측한다. control deadline이 실제 발표/리허설 창과 맞는지 승인 후 readback한다.

<a id="gap-032"></a>

### GAP-032 — 비용 ledger는 수동 관측이며 실행 예산을 차단하지 않는다

- 우선순위/분류/상태: **P1 · MISSING · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `KNOWN_NOT_IMPLEMENTED`.

**사용자 계약:** 실제 비용을 추적하고 5만 원 목표 초과가 예상되면 새 유료 작업을 관리자 승인 대기로 전환한다. 계획 `platform/CONTROL-PLANE-PLAN.md:658`, `:680`–`:686`.

**현재 코드:** `platform/infra/costs.py:203`–`:231`은 GCP/Azure export 수동 import/report/reserve/release CLI다. AWS 청구 importer는 없다. `:163`–`:200`의 reservation은 원장 transaction이며 provision/runner/VM 생성이 이를 호출하지 않는다. 자동 collector·견적·집행 연동 없음은 `platform/infra/README.md:3`, `:112`에 명시돼 있다. AWS budget `main.tf:157`–`:169`는 선택적 월간 forecast email이고 새 작업 금지 장치가 아니다.

**영향/최소 조치:** 현재 관리자 AWS 사용액과 보존 disk/IP까지 포함한 전체 비용/잔여 예산을 자동 보장하지 못한다. 실제 선택 provider의 비용 snapshot·resource inventory를 수집하고, task/infra operation이 생성 전에 동일 operation ID의 hold를 얻도록 연결한다. 청구 지연·unknown은 실행 예산 0으로 취급하지 않는다.

**완료 인수:** AWS 포함 실제 운영 범위의 export를 원본 행 수/시각과 대조하고, stale/missing/통화 불일치/한도 초과에서 새 paid dispatch가 0회다. 동시 요청과 결과 유실에서도 hold가 유지되고, retained disk/IP는 compute stop 후 합계에서 사라지지 않는다.

<a id="gap-033"></a>

### GAP-033 — 앱 replica 확장과 VM 용량 관리 사이 자동 조정은 없다

- 우선순위/분류/상태: **P2 · MISSING · OPEN · SOURCE_CONFIRMED(실클라우드 미실행)**
- 확인 시각: 2026-10-01 18:33:02 KST 기준 소스 대조; 보고서 저장 18:38:23 KST.
- 증거: 관리자 증거의 `product-infra-source-manifest.json`.

- 이전 상태 구분: `KNOWN_NOT_IMPLEMENTED`.

**사용자 계약:** 사용자는 VM 사양/비용을 확인하고 필요한 자원을 편하게 관리한다. 현재 계획은 bounded 앱 확장과 승인된 노드 크기 변경을 구분한다.

**현재 코드:** `platform/infra/specsheet.py:109`–`:114`는 descriptor에 없는 CPU/메모리·전원 상태를 unknown으로 유지한다. `:136`–`:143`은 KEDA renderer 구현과 node_scale_out not_implemented를 명시한다. `platform/infra/README.md:116`–`:118`의 resize는 수동 유지보수 절차이며 GCP `main.tf:110`은 자동 stopping-for-update를 금지한다. Terraform 변수 변경/하드코딩 size allowlist는 live capacity discovery나 자동 resize가 아니다.

**영향/최소 조치:** KEDA fixture 1→3→1 성공을 VM 부족 대응·node auto-scale·일반 앱 성능 보장으로 확대하면 안 된다. PoC는 node scale-out을 제외한다고 명시하고, 실제 사용량/CPU credit/disk/Pod Pending 관측→관리자 검토 계획→drain/resize→readiness를 먼저 연결한다. 비용 API catalog를 추정 하드코딩해 메우지 않는다.

**완료 인수:** 실제 VM spec·관측 시각·resource usage를 표시하고, saturation/Pending에서 무단 증설 없이 차단/승인 요청한다. 승인한 크기 변경 후 데이터·node·앱 상태를 확인하며 단일 노드의 중단 시간을 기록한다.

<a id="gap-034"></a>

### GAP-034 — 네트워크 보안 프로파일이 gate·CSP 전체에 적용되지 않음

- 우선순위/분류/상태: **P1 · MISSING/UNVERIFIED · OPEN · SOURCE_CONFIRMED, LIVE_NOT_RUN**
- 확인 시각: 2026-10-01 18:43 KST. 이전 문서의 제한 재확인.
- 근거: `platform/gate/gate.py:456`의 Trivy 실행은 Docker socket을 mount하고 Q/L2/L3와 달리 명시 network·CPU/memory/PID profile을 사용하지 않는다. CI public 80/443/DNS는 registry 도메인 allowlist가 아니다(`infra/ansible/ci.yml:138`). GCP/Azure 노드 모듈에는 AWS SG와 같은 outbound allowlist가 없고 플랫폼 일부 namespace도 기본 egress 허용이다. `platform/ZERO-TRUST.md`와 `platform/scenarios/ci-pipeline.md`는 한계를 이미 명시했다.
- 영향: “모든 gate와 CSP에서 동일한 egress/자원 정책”으로 일반화할 수 없다. 현재 새 Q/L2/L3 helper+profile의 실제 Linux Docker 경로와 전체 Cilium baseline+KEDA는 미검증이다. exploit이나 실제 유출을 재현한 것은 아니다.
- 최소 개선/인수: 관리형 scanner의 권한/자원 profile을 분리해 제한하고, provider capability별 강제 가능한 정책을 명시한다. 같은 소스/profile hash에서 Q/L2/L3/L4의 metadata·host·private deny, 필요한 public/peer 허용, 부팅 후 검증 receipt 유효성을 실제 시험한다.

<a id="gap-035"></a>

### GAP-035 — DB 선택 도구가 실제 intake·schema·배포 경로에 연결되지 않음

- 우선순위/분류/상태: **P1 · MISSING · OPEN · SOURCE_CONFIRMED/CONTRACT_ONLY**
- 확인 시각: 2026-10-01 18:43 KST. 기존 명시적 미구현 재확인.
- 근거: `platform/infra/database.py:2`는 외부 evidence 요약을 받는 오프라인 선택기이며 파일 진위·연결을 확인하지 않는다. `database.py:117`은 SQLite renderer 미구현, `database.py:133`은 managed renderer 미구현을 반환한다. intake는 이를 호출하지 않고 schema에는 PostgreSQL small만 있다. `platform/scenarios/db-migration.md:9` 및 `:113`에 미연결 범위가 명시돼 있다.
- 영향: 사용자 코드로 DB를 확인하고 SQLite 유지/관리형 RDB/컨테이너 Postgres를 실제로 선택·배포하는 UX가 없다. SQLite 단일 writer·PVC와 관리형 DB의 TLS·5432·runtime/migration Secret, 데이터 이관·이전 schema 호환성까지 준비된 것으로 볼 수 없다.
- 최소 개선/인수: 증거 생산자인 intake에서 실제 파일/연결 계약과 source hash를 만들고, 지원 mode만 schema→render→gate에 연결한다. SQLite restart 데이터 유지/scale-out 차단, 기존 DB migration 호환성, 외부 DB 최소 연결을 인수한다. 백업 lifecycle은 GAP-007과 구분한다.

<a id="gap-036"></a>

### GAP-036 — 이미지 publish의 부분 부작용이 공통 관측·복구 계약 밖에 있음

- 우선순위/분류/상태: **P1 · DEFECT/NAIVE · OPEN · REPRODUCED_LOCAL(경계 모의 처리)**
- 확인: 2026-10-01T18:41:41.907893+09:00 / 09:41:41.907893Z.
- 근거: `platform/gate/bundle.py:150`은 image별 push를 순서대로 수행하고 `bundle.py:190`–199는 모든 publish가 끝나야 images.json을 저장한다. 공통 OperationError/event나 durable per-image receipt가 없다. 첫 image push 완료·두 번째 push 실패를 주입하면 exit 2, 출력 receipt 없음, `bundle rejected: Docker operation failed: image push`만 남았다.
- 영향: 실제 부분 발행 또는 push 응답 유실이 입력 거부와 구분되지 않는다. 재시작 시 완료된 발행과 불확실한 발행을 대조할 구조가 없어 관측성·resume의 적용이 끊긴다. 실제 registry push는 0회이며 이 결과는 제어 흐름 재현이다.
- 최소 개선/인수: 기존 공통 오류 계약을 사용하고 operation/image digest별 의도·관측 receipt를 내구성 있게 남긴다. 두 번째 image 실패·push 응답 유실·receipt 쓰기 실패 뒤 완료/UNKNOWN을 구분하며 재빌드 없이 registry 실제 digest와 대조한다.
- 증거: `bundle-partial-publish.json`.

<a id="gap-037"></a>

### GAP-037 — 공통 정책이 아직 여러 실행기에서 독립 상수로 반복됨

- 우선순위/분류/상태: **P2 · DUPLICATION/NAIVE · OPEN · SOURCE_CONFIRMED**
- 확인 시각: 2026-10-01 18:43 KST.
- 근거: 필수 gate 순서가 `platform/gate/gate.py:37`, `platform/gate/bundle.py:24`, `platform/loop/loop.py:247`, `loop.py:281`에 독립 선언돼 있다. SDK version은 runner profile, CI workflow `ci/railshot-deploy.yml:98`–99, control bootstrap에 별도로 pin된다. workflow의 동일 target regex도 여러 job에 복사돼 있다. 현재 값이 어긋난다고 주장하는 것은 아니다.
- 영향: 한 실행기에 추가한 gate/버전/target 정책이 다른 소비자에서 누락되거나 불필요하게 거부될 수 있다. BuildKit profile 한 곳을 공통화한 것으로 플랫폼 정책 전체가 일원화됐다고 말하면 안 된다. 별도 의미를 가진 checkpoint hash와 artifact hash는 단순 중복으로 합치지 않는다.
- 최소 개선/인수: 의미가 같은 policy 값만 기존 공통 계약에서 읽는다. 보안 경계의 독립 재검증은 유지하고, 모든 소비자가 같은 policy/version을 사용하는 계약 검사를 둔다. 동적 plugin framework나 모든 숫자의 설정화는 필요하지 않다.

<a id="gap-038"></a>

### GAP-038 — 필수 품질 gate와 전체 지원 옵션의 인수 증거가 아직 닫히지 않음

- 우선순위/분류/상태: **P2 · MISSING/UNVERIFIED · OPEN · SOURCE_CONFIRMED/CONTRACT_ONLY**
- 확인 시각: 2026-10-01 18:43 KST. 기존 제한 재확인.
- 근거: `platform/scenarios/ci-pipeline.md`는 source SAST, coverage 기준, 전체 dependency/SBOM/provenance, API·브라우저 E2E를 필수 gate로 구현하지 않았다고 명시한다. pnpm/Yarn workspaces·복합 JVM·일부 reporter/JDK도 제한된다. 실제 GitHub/CodeBuild 제품 release, 보호 CD worker, live Pod identity·revision-bound HTTP, Claude 실제 호출과 native conversation resume의 완료 증거는 없다. runner native resume는 명시적으로 거부한다.
- 영향: 현재 “명시적으로 제한한 일부 profile의 과거 fixture 실행 + 로컬 회귀”를 모든 옵션 E2E 완료로 바꿀 수 없다. 특히 GAP-013의 약한 negative oracle 때문에 Java 원인 일치 주장은 다시 확인해야 한다. 유료 Claude 실호출은 사용자 잔여/지시 범위와 별개로 자동 수행하지 않는다.
- 최소 개선/인수: 지원/차단 표와 증거 수준을 고정하고 잘못된 oracle 수정 후 같은 소스 hash로 해당 positive/negative를 재검사한다. 추가 품질 gate는 이름만 추가하지 말고 실제 도구·기준·실패 fixture와 함께 도입한다. 미지원은 명시적으로 거부한다.

## 이전 수정 유지와 이번 감사의 한계

- `loop.gate()`의 stdout UNKNOWN 우선 보존은 stale disk PASS보다 우선하며 독립 재확인에서도 유지됐다. GAP-017은 agent receipt의 원인 유실로, 같은 gate 결함의 재발이나 PASS 승격을 주장하지 않는다.
- 관측 오류의 `<stdin>` 가상 위치 정규화/왕복은 재확인됐다. in-flight 불확실 상태의 자동 SDK 재실행 차단도 유지된다. 단순한 빈 예외 클래스 자체를 결함으로 세지 않고, 실제 cause·outcome·retry·side-effect 정보가 사라지는 소비 경계를 판정했다.
- Codex native permissions, Claude tool-time read guard, 비밀 파일명 deny가 존재한다. 이번 감사에서 새 read-policy 우회를 입증하지 않았다. 내용 기반 비밀 탐지 전체를 검증한 것도 아니다.
- native SDK conversation resume, 원격 dispatch/cancel/reconcile, 다중 worker fencing, 독립 감사 저장소/retention/OTel collector, 다중 사용자 모델 인증은 문서의 미구현/제한 범위다. 현재 로컬 checkpoint·SQLite·프로필 선언으로 이 제품 기능들이 완성됐다고 판단하지 않는다. 포괄적인 미구현 목록을 각자 새 결함으로 추가 집계하지 않았다.
- 과거 로컬 회귀 190건은 그 실행 시점의 테스트 범위에 대한 증거다. 이번 감사는 전체 suite를 다시 실행하지 않았다. 신규 실패 조건과 GAP-013의 잘못된 원인 판정은 그 숫자로 상쇄되지 않는다.
- CI 7개 사례와 runner 9개 사례 묶음을 부모 작업에서 독립 재실행했다. runner 9개는 신규 8개 결함 재현과 기존 수정 유지 1개 묶음이다. assertion 통과는 결함을 재현했다는 뜻일 수 있으며 제품 PASS와 반대다.
- 이번에는 실제 Docker/registry push, AWS/GCP/Azure 변경, 모델 호출, CodeBuild/GitHub release, live cluster cold-start/삭제/부하 시험을 실행하지 않았다. 메모리 OOM·권한 탈취·데이터 유실·remote cancellation은 실제 발생으로 기록하지 않았다.
- CI 재현은 이미 설치된 offline 의존성을 사용했다. renderer 첫 host Python 실행은 jsonschema 부재로 진행하지 못해 기존 check venv로 재실행했다. runner 첫 fault injection은 macOS `/var`와 `/private/var` 경로 차이를 보정해 재실행한 receipt만 채택했다. 도구 환경 실패와 제품 결함을 분리했다.

## 수정 순서와 닫는 기준

1. **판정 신뢰성:** GAP-009/012/013의 거짓 성공·오분류·약한 음성 oracle을 먼저 수정한다. 검사가 실제 실행되지 않은 fixture와 다른 원인 실패를 구분한 뒤 기존 Java 검증 주장을 재평가한다.
2. **부작용과 복구:** GAP-016–023/030/036을 기존 공통 오류·state 경로에서 수정한다. 시간 초과, 부분 쓰기, 마지막 기록 실패, 재기동을 주입해 실제 부작용과 보고된 상태/복구 정책을 대조한다. 로컬 종료·checkpoint를 원격 취소/정확히 한 번 실행으로 승격하지 않는다.
3. **CI/CD와 데이터·공급망 계약:** GAP-001–008/010/011/014/027/028/029/034를 다룬다. 동일 이미지 실행 의미, 요청-산출물 일치, 깨끗한 렌더, 고정 bootstrap, 데이터 보존, 실제 Linux egress를 검증한다.
4. **제품 연결:** GAP-015/024–026/031–033/035를 실제 API→worker→할당/예산→VM/CI/CD→화면 관측에 연결한다. YAML·시안·CLI만으로 완료 표시하지 않는다. PoC에서 제외할 지원 옵션은 명시적으로 차단한다.
5. **중복 정책과 E2E:** GAP-037의 같은 의미의 값만 공통화하고 GAP-038의 지원 표를 고정한다. 실제 지원 조합에 대해 positive/negative·권한·중단/재개·배포 identity 증거를 확보한다. 미지원/미실행을 PASS와 섞지 않는다.

닫을 때는 `resolved_at`, 변경 파일/commit, 확인한 소스 hash, 테스트·실행 receipt, 남은 한계를 이 원장에 추가한다. 증거 없이 OPEN을 지우거나 정상 fixture 하나로 범용 지원을 선언하지 않는다. 새 감사 프레임워크나 중복 backlog를 만들 필요는 없다.

## 보존된 증거

관리자 증거 root는 위의 `~/.local/state/railshot/audits/2026-10-01/gap-review-182949/`이며 디렉터리 mode는 0700이다. 다음 파일은 root 기준 상대 경로다. 이 원장을 Git에 보존해도 로컬 증거가 함께 업로드되는 것은 아니다.

| 파일 | 용도 |
|---|---|
| `source-snapshot.json` | 시작 시 branch/base·dirty 상태·관련 134개 파일 SHA256 |
| `audit-summary.json` | 종료 시각·원장 목록/우선순위·기존 소스 불변 확인·증거 hash |
| `render-reproduction.json`, `reproduce-render.py` | migration/bucket/잔존 YAML 실제 렌더 재현 |
| `bootstrap-failure.json`, `route-probe.json` | SSM 실패 분기와 경로 불일치 확인 |
| `ci-reproduce.py`, `ci-reproduce.json`, `parent-ci-recheck.json` | CI 7개 사례 및 독립 재확인 |
| `runner-state-repros.py`, `runner-state-repros.json`, `parent-runner-recheck/runner-state-repros.json` | runner/state 사례 및 독립 재확인 |
| `product-infra-source-manifest.json` | 제품/인프라 소스 27개와 콘솔 inventory |
| `bundle-partial-publish.json` | push 경계 모의 처리로 부분 완료 후 receipt 부재 확인 |
| `ci-findings.md`, `runner-state-findings.md`, `product-infra-findings.md` | 분야별 원본 감사 보고서; 상태 정본은 이 GAP 원장 |

## 변경 기록

| 시각(KST) | 내용 |
|---|---|
| 2026-10-01 18:29:49 | 감사 시작 |
| 2026-10-01 18:31:08 | 기존 working tree 134개 관련 파일 hash snapshot 저장 |
| 2026-10-01 18:33:12 | renderer 실제 로컬 재현 3건 저장 |
| 2026-10-01 18:36:36 | CI 7개 오프라인 사례 재현 저장 |
| 2026-10-01 18:38:23 | 제품/인프라 소스 감사 보고서 저장 |
| 2026-10-01 18:38:27 | runner/state 8개 결함 및 기존 수정 유지 묶음 재현 저장 |
| 2026-10-01 18:39:41–42 | CI 및 runner/state 사례 부모 작업에서 독립 재확인 |
| 2026-10-01 18:41:41 | 부분 publish receipt 누락 모의 재현 저장 |
| 2026-10-01 18:50:28 | 38개 OPEN 항목으로 통합; 기존 관련 소스 134개 hash 불변 확인; 제품 코드 미수정 |
