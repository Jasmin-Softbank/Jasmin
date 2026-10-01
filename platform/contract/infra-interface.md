# 인프라 상위 인터페이스 v0.1

2026-10-01 · **구현 제안**. 팀에서 정한 `create_infra`, `read_infra`, `update_infra`, `delete_infra` 이름을 유지하고, 제품 작업 흐름에 필요한 입력·결과·실패 의미를 정의한다. §8은 다른 CSP를 연결하는 컨벤션, §9는 Terraform·Ansible의 제품 운영 계약이다. HTTP 경로와 추가 필드는 이번 제안이며 서버 구현·팀 채택 완료를 뜻하지 않는다. 원자료는 복사하지 않고 관련 합의 지점만 연결한다.

- 팀 기준: [Provider Interface](https://app.notion.com/p/3ea8bee9ada48016b4cfd3bf2f8410b9), [온프레 REST Controller 설계 v0.2](https://app.notion.com/p/3eb8bee9ada4804c8dd3c683efa3269e).
- 제품 흐름·권한·검증 순서: [CONTROL-PLANE-PLAN.md](../CONTROL-PLANE-PLAN.md).

## 1. 위치와 책임

```text
웹 콘솔 / MCP / 에이전트의 제안
              ↓ 인증·소유권 검사
기존 제품 API + worker
  요청 해석 → 대상 capability 확인 → 변경 계획 → 정책/Allow → 영속 operation
              ↓ CRUD_infra
  Terraform 실행 / AWS workspace API / 온프레 Controller 어댑터
              ↓ 실제 상태 관측
  InfraResource + 준비 조건 → CI 또는 배포 단계에 전달

CI 통과 artifact → 별도 release 승인 → GitOps commit → Argo CD → 배포 검증
```

상위 계층은 기존 API/worker 안에 둔다. 별도 gateway 서비스·메시지 브로커·범용 워크플로 엔진을 신설하지 않는다. 어댑터는 내부 함수이고, 온프레의 기존 HTTP Controller만 원격 호출한다. 상위 계층이 인증, 사용자별 기본값, 순서, 승인, 영속 상태, 재시도와 부분 실패를 소유한다. 하위는 단일 provider 작업의 검증·실행·현재 상태 조회를 소유한다.

**앱 배포는 인프라 CRUD가 아니다.** `create_infra` 성공으로 앱 배포 완료를 표시하지 않는다. Terraform은 기반 자원, Ansible은 승인된 OS/서비스 구성, Argo CD는 Git의 Kubernetes 선언을 담당한다. VM의 전원, CI job, 앱 workload, 영속 데이터는 별도 수명을 갖는다.

## 2. 네 함수와 HTTP 경계

내부 함수 형태는 `create_infra(context, plan)`, `read_infra(context, resource_id)`, `update_infra(context, resource_id, plan)`, `delete_infra(context, resource_id, plan)`이다. 쓰기 함수는 이미 검증한 불변 계획을 받으며 모델 출력이나 임의 HCL을 직접 실행하지 않는다. 하위 Controller DTO를 전면 교체하지 않고 어댑터에서 변환한다.

| 기능 | 상위 API 제안 | 결과 |
|---|---|---|
| 지원 범위·프로필 조회 | `GET /api/v1/infra/targets/{target_id}/capabilities` | 권한 있는 대상의 기능·제약·프로필 목록, 버전/hash, 관측 시각 |
| 계획 준비 | `POST /api/v1/infra/plans` | `202` + 계획 생성 operation; 자원 생성은 하지 않음 |
| 계획 읽기 | `GET /api/v1/infra/plans/{plan_id}` | 완료된 변경 요약·정규화 spec·hash·유효 기한·영향 |
| `create_infra` | `POST /api/v1/infra/resources`, body `{ "plan_id": "…" }` | `202` + operation, 미리 예약한 논리 resource ID |
| `read_infra` | `GET /api/v1/infra/resources/{resource_id}` | `200` + desired/observed·conditions·ETag |
| `update_infra` | `PATCH /api/v1/infra/resources/{resource_id}`, body `{ "plan_id": "…" }` | `202` + operation; 시작·중지도 여기서 계획의 desired power를 적용 |
| `delete_infra` | `DELETE /api/v1/infra/resources/{resource_id}?plan_id=…` | `202` + operation; DELETE body는 사용하지 않음 |
| 진행·결과 조회 | `GET /api/v1/infra/operations/{operation_id}` | `200` + 상태·결과 참조·실패 정보 |

`202` 응답은 `Location: /api/v1/infra/operations/{id}`와 `Retry-After`를 포함한다. SSE는 같은 저장된 상태의 변경 알림이며, 끊기면 GET으로 복구한다. 접수는 성공·준비 완료를 뜻하지 않는다. 계획 생성 실패도 해당 operation에서 확인한다. 조회 범위 밖 ID는 `404`, 인증 실패는 `401`, 인증된 호출자의 기능 권한 부족은 `403`이며 모두 provider 호출 전에 판단한다.

API는 OpenAPI **3.1.1**과 그 JSON Schema 방언으로 명세화한다. 이는 호환 기준 선택이며 최신 버전이라는 주장이 아니다. 기존 앱 spec의 draft-07 schema를 이 변경 때문에 일괄 변환하지 않는다. `202`, 조건부 변경의 `If-Match`/`412`는 HTTP 의미를 따른다. [OpenAPI 3.1.1](https://spec.openapis.org/oas/v3.1.1.html), [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html).

## 3. 데이터 계약

| 객체 | 필수 정보와 독자 |
|---|---|
| RequestContext | API가 인증 결과로 만든 `request_id`, `principal_id`, `tenant_id`, `project_id`, `target_id`. provider 계정/프로젝트 매핑은 서버 설정에서 결정한다. 어댑터가 사용하며 외부 입력값을 신뢰하지 않음 |
| TargetCapabilities | `version`, `observed_at`, 지원 resource kind/action, 허용 profile·image·network 참조, 전원/보존/백업 지원 여부. planner가 지원 불가 작업을 실행 전 거부 |
| InfraPlan | `plan_id`, `action`, resource/target, 정규화 spec, profile/capability 버전, 현재 resource generation, 생성/변경/교체/삭제 목록, 데이터·중단 영향, 비용 가정, artifact 참조·hash, `plan_hash`, `expires_at`. 승인 UI와 executor가 같은 값을 읽음 |
| InfraResource | 논리 `resource_id`, 소유자, `kind`, `target_id`, `execution_driver`, `desired_generation`, desired, observed, `observed_at`, conditions, provider의 ID들, 생성 operation, 보존 정책. API/allocator가 수명 관리 |
| Operation | `operation_id`, 요청 kind, resource/plan ID, idempotency key/hash, 상태, stage, 결과 참조, provider request ID, 시각, redacted error. worker가 dispatch 전에 기록하고 UI가 조회 |
| ReadyTarget | `target_id`, `cluster_ref`, 허용 namespace/AppProject, ingress·storage·secret profile 참조, 이미지 architecture, 관측 경로. 신뢰된 renderer/release가 읽으며 클러스터 관리자 자격은 담지 않음 |

사용자 입력은 `workspace` 또는 `app_cluster` 같은 허용 kind, 관리자가 등록한 `profile_ref`, 대상 alias와 필요한 desired 변경으로 제한한다. CPU는 vCPU 정수, 메모리는 MiB, 디스크는 GiB로 정규화한다. 공급자 계정 ID, secret 값, HCL, 셸, cloud-init, 임의 image/network ID를 사용자 요청에서 받지 않는다. profile을 실제 AWS instance type 또는 OpenStack flavor/image/network에 해석한 결과를 계획에 고정한다. 정확한 조건을 만족하는 항목이 없으면 막고, 몰래 큰 유료 flavor로 올리지 않는다.

비용은 `amount`, `currency`, `horizon_hours`, `assumptions`, `quoted_at`로 제시한다. 온프레처럼 사용량 원가를 알 수 없으면 `amount: null`과 이유를 기록한다. 미지원·미관측을 비용 0 또는 정상으로 바꾸지 않는다. 동일 profile alias를 편집해도 진행 중 계획은 이전 profile hash를 유지하며 변경된 환경에서 실행 가능한지 다시 확인한다.

### 상태와 준비 조건

- Operation: `queued → running → succeeded | failed | outcome_unknown`. `outcome_unknown`은 결과 확인 대기이며 자동 재전송 상태가 아니다. readback으로 결과를 확인하면 종료 상태로 이동한다.
- 중단 요청은 별도 `cancel_requested_at`에 저장한다. 아직 dispatch하지 않았다면 `cancelled`; 이미 provider가 받은 작업은 현재 단계를 확인하고 후속 단계만 중단한다. 작업 취소가 인프라 삭제·Terraform 강제 unlock을 뜻하지 않는다.
- InfraResource는 desired와 observed를 구분한다. observed power는 `pending/running/stopped/terminated/unknown`, 원래 provider status도 보존한다. 조건은 `ComputeReady`, `BootstrapReady`, `RunnerReady`, `DesktopReady`, `ClusterReady` 중 해당 kind에 필요한 것만 사용하며 각 값은 `true/false/unknown`과 시각·근거를 갖는다.
- VM 생성 operation의 성공은 공급자 생성 완료까지만 뜻한다. workspace CI dispatch에는 Compute/Bootstrap/RunnerReady, 화면 연결에는 DesktopReady, 배포 대상에는 ClusterReady가 추가로 필요하다. OpenStack `ACTIVE`나 EC2 `running`만으로 다음 단계를 열지 않는다.
- 상태 조회 장애는 `unknown/stale`이다. provider의 권한 있는 조회로 부재를 확인한 경우만 `not_found`로 처리한다. 앱의 `DeploymentHealthy`는 별도 deployment receipt에서 판정한다.

## 4. 계획·승인·중복 방지

1. 계획을 준비하며 권한, 예산, capability, 대상 revision, quota를 검사한다. `plan_hash`는 정규화 spec, target, resource generation, 실행 profile/도구 버전, 실제 provider 변경 artifact hash, 데이터 정책을 묶는다. 초기 유효 기한은 10분을 제안하며 실제 구현에서 명시적으로 고정한다.
2. 상위 정책이 `allowed`, `approval_required`, `denied`를 저장한다. 이미 허용된 범위의 workspace 기동마다 팝업을 만들지 않는다. 공개 배포·증설·교체·영속 데이터 삭제 등은 사용자/관리자의 해당 권한을 검사한다. 승인 주체·범위·plan hash를 서버에 결합하고 `approved: true` 같은 클라이언트 필드는 받지 않는다.
3. mutation은 `Idempotency-Key`를 필수로 받는다. `(tenant, principal, action, key)` unique 제약과 정규화 요청 hash로 같은 요청은 기존 operation을 반환하고, 같은 key의 다른 내용은 `409 idempotency_conflict`로 거부한다. 이 헤더의 보존·동작은 **우리 API 계약**이며 이를 확정된 IETF 표준이라고 부르지 않는다. PoC 기간에는 key 기록을 지우지 않는다.
4. update/delete는 resource GET의 strong ETag를 `If-Match`로 받아 오래된 generation이면 `412`로 거부한다. 조건부 요청이 없으면 `428`로 거부한다. ETag는 상위 자원 표현의 버전이며 provider drift 검사를 대신하지 않는다. 동일 key로 이미 접수한 요청의 재조회는 이전 operation을 돌려주되 매번 소유권을 확인한다. [428 의미: RFC 6585](https://www.rfc-editor.org/rfc/rfc6585.html#section-3).
5. API는 서로 다른 key의 요청도 **resource별 mutation claim + generation CAS**로 직렬화한다. 동일 generation의 stop/delete 등이 경쟁하면 하나만 claim을 얻고 나머지는 `409 resource_busy`다. worker는 operation, 승인 소비, 실행 claim을 DB transaction으로 저장한 다음 외부 호출하며 dispatch와 결과 반영 때 claim의 fencing generation을 재검사한다. unresolved operation이 있으면 충돌 변경을 차단한다. 생성 결과가 불명확한 동안에도 사용자별 workspace 배정·전체 VM 슬롯·예산 예약을 유지하고, 새 key의 생성 요청은 기존 배정으로 연결하거나 충돌 처리한다. 호출 전후 중단에 대비해 동일 논리 작업의 provider correlation을 기록한다. 외부 변경의 exactly-once를 DB 기록 하나로 보장한다고 주장하지 않는다.
6. 하위의 idempotency 기능이 있으면 재사용한다(AWS 생성 ClientToken 등). 하위에 기능이 없고 응답이 유실됐다면 ID/correlation 조회로 결과를 먼저 확인한다. 고유하게 식별할 방법이 없으면 `outcome_unknown`, `retryable: false`로 관리자 확인을 기다린다. 같은 이름으로 새 VM을 다시 만드는 방식은 금지한다.
7. 승인 후 소스·spec·대상 상태·정책·artifact가 바뀌거나 계획이 만료되면 재계획한다. 새 hash에 기존 Allow를 붙이지 않는다. 인프라 승인과 앱 release 승인은 서로 다른 operation이다.

오류는 `application/problem+json`의 `type`, `title`, `status`, `detail`, `instance`와 확장 필드 `code`, `request_id`, `operation_id`, `retryable`, `outcome_unknown`을 사용한다. `capability_unsupported`, `plan_expired`, `budget_exceeded`, `provider_unavailable`, `dispatch_outcome_unknown` 등을 안정된 code로 분류한다. 비밀·provider traceback·state 원문은 반환하지 않는다. [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html).

## 5. 실행 경로별 매핑

| 경로 | CRUD 구현 범위 | 완료/복구 관측 |
|---|---|---|
| Terraform 기반 자원 | 검토된 모듈의 VPC/SG/IAM/Launch Template/앱 노드. create/update/delete는 저장한 plan의 해당 변경만 적용 | state lineage/serial과 lock, apply 결과, provider 실제 자원 조회, 별도 bootstrap 결과 |
| AWS 개인 workspace | 고정 Launch Template으로 EC2 생성, describe, start/stop, terminate. allocator가 sole owner | instance/volume ID, 동일 ClientToken, EC2 상태, 서비스 heartbeat, runner 등록 |
| 온프레 Controller | 기존 `/api/v1/servers`와 server actions에 생성·조회·삭제·start/stop을 매핑. 이미지/flavor/network catalog 재사용 | 하위 `Accepted.resource_id`로 poll, BUILD/ACTIVE/ERROR 원문 보존; 상위에서 영속 operation을 추가 |

**한 자원은 한 실행 경로만 소유한다.** allocator의 동적 EC2를 Terraform state에도 넣지 않는다. `execution_driver`는 생성 때 고정한다. 소유권 이전은 별도 관리자 이전 계획 없이는 허용하지 않는다. 일상적인 앱 변경은 이 세 경로의 인프라 수정 없이 GitOps만 갱신한다.

### Terraform

관리자 전용 실행 환경에서 `plan -out`으로 저장한 정확한 artifact를 apply한다. 승인에는 모듈 commit, Terraform/provider lock, 변수 참조 버전, 대상 state 식별, plan hash를 묶는다. plan/state는 secret을 포함할 수 있어 암호화된 제한 저장소에 두고 로그·Git·사용자 VM·브라우저에 노출하지 않는다. 사용자에게는 필요한 변경 요약만 제공한다. [HashiCorp plan 문서](https://developer.hashicorp.com/terraform/cli/commands/plan).

같은 state에 쓰기는 한 개만 허용하고 backend lock을 사용한다. plan 이후 state나 실제 자원 조건이 달라지면 기존 plan을 자동 재생하지 않고 재계획·재승인한다. 실행 도중 장애가 나면 lock 소유자와 실제 자원을 확인한다. 자동 `force-unlock`·`state rm`·전역 destroy로 복구하지 않는다. 이미 적용된 일부 변경은 readback으로 알려진 ID와 함께 남긴다.

### 온프레

기존 Controller의 `RequestContext`, `CreateServerSpec(name,image_id,flavor_id,network_ids)`, `Accepted`, `DeleteResult(already_absent)`를 존중한다. 각 하위 요청의 `request_id`는 operation ID·idempotency key와 다른 값이다. 하위의 `202 + Location(resource)`를 상위의 `202 + Location(operation)`으로 감싼다. 하위가 아직 영속 operation/idempotency를 제공하지 않으므로 상위 재시도만으로 안전한 생성 재전송이 해결됐다고 하지 않는다.

첫 연동의 지원 범위는 서버 CRUD 중 실제 가능한 생성·조회·삭제와 전원 변경, 기존 image/flavor/network 조회다. `update_infra`의 크기 변경·디스크 추가·network 변경은 capability에 명시되지 않으면 거부한다. soft reboot는 현재 제품 필수 경로가 아니므로 상위에 별도 함수로 노출하지 않는다.

새 network, floating IP, Cinder volume, LB, snapshot/restore는 담당자 명세와 구현이 확인되기 전 **미지원**이다. flavor root disk를 보존 볼륨으로 간주하지 않는다. 따라서 온프레의 영속 workspace/DB 요구를 만족할 저장소 계약이 없으면 그 profile의 데이터 보존 약속을 막는다. 정적 앱의 온프레 배포 검증과 영속 데이터 기능 검증을 구분한다. Proxmox 등 별도 provider를 이미 지원한다고 표시하지 않는다.

## 6. 부분 실패·삭제·배포 인계

다중 자원 요청은 상위 worker가 순서대로 처리하며 각 자원 ID·operation을 남긴다. 한 단계 실패하면 후속 dispatch를 중단하고 성공한 자원과 미완료 단계를 반환한다. 이미 생성된 자원을 일괄 없애는 보상 삭제는 기본값이 아니다. 정리가 필요하면 **이번 operation이 생성했고 단독 소유하는 자원만** 별도 cleanup plan으로 제시한다. 기존 네트워크·공유 클러스터·DB는 정리 대상에 넣지 않는다.

VM stop은 compute만 멈추고 지원되는 영속 볼륨과 workspace ID를 보존한다. RAM·실행 프로세스 복원은 보장하지 않는다. delete 계획은 compute, volume, snapshot, address 각각의 보존/해제 결과를 표시한다. 데이터 삭제는 별도 명시 승인과 필요한 백업 검증을 거친다. compute 삭제 후 retained volume은 인벤토리·비용에 계속 남기며 부모 기록의 tombstone에서 참조한다. 앱 Pod·workload Application 삭제가 이 API를 자동 호출하지 않는다.

stop/delete 실행기는 승인 여부와 별도로 **drain 완료를 필수 선행 조건**으로 검사한다. 신규 dispatch 차단, 활성 CI·사용 lease 확인, checkpoint/artifact 저장, runner 정리를 마친 뒤 provider를 호출한다. 실행 직전 다시 검사해 새 작업이 있으면 기다리거나 거부한다. app-cluster compute 중지/삭제는 사용 중 앱·DB/PVC 의존성과 정지·보존 근거까지 확인한다. 백업이 있다는 사실만으로 실행 중 데이터 서비스를 끊지 않는다. 비용 마감의 강제 cutoff는 별도 명시 정책으로 다루고, 중단 작업은 `interrupted`로 기록해 다음 기동 때 대조한다.

앱 대상 준비 후 ReadyTarget을 renderer/release에 전달한다. 신뢰된 관측 수집기는 `deployment_id`, `gitops_revision`, Application operation/sync/health, live image digest와 workload generation/replicas, 해당 버전 외부 probe를 보고한다. 사용자의 작업 VM은 자신의 앱 관측 결과만 읽는다. Argo cluster-admin 토큰·Terraform 자격을 넘기지 않는다.

온프레/클라우드의 공통 배포 입력은 `source_revision`, `spec_hash`, `image_digest`, `target_id`, `resource_profile`, `secret_refs`다. 데이터 공급자가 CNPG/RDS인지, secret이 Sealed Secrets/ESO인지, ingress가 cloudflared/직접 Gateway/ALB인지의 차이는 **고정된 target profile**로 선택한다. 지원하지 않는 조합을 조용히 대체하지 않는다. LKG는 target+app 단위이며 공유 GitOps 저장소의 `HEAD` 전체를 되돌리지 않는다.

## 7. 예시와 계약 검증

새 workspace의 계획 요청 예시다. ID·용량·결과는 예시이며 실제 자원 생성 증거가 아니다. `project_id`는 경로/인증 문맥에서 소유권을 검사하고 provider 프로젝트로 서버에서 매핑한다.

```json
{
  "action": "create",
  "kind": "workspace",
  "project_id": "project-demo",
  "target_id": "aws-seoul-poc",
  "profile_ref": "workspace-ci-v1",
  "desired": { "power": "running", "data_policy": "retain" }
}
```

계획 operation 완료 → 계획과 비용/영향 표시 → 서버 정책 또는 필요한 Allow 기록 → `POST /api/v1/infra/resources`에 해당 `plan_id` 전달 → 아래 접수 응답을 읽는다.

```json
{
  "operation_id": "op-example-create",
  "resource_id": "workspace-example",
  "state": "queued",
  "status_url": "/api/v1/infra/operations/op-example-create"
}
```

실제 생성 후에도 `RunnerReady`가 false이면 UI는 “컴퓨터 준비 중”이고 CI를 보내지 않는다. progress percent는 provider가 신뢰 가능한 값을 제공할 때만 기록하며 시간으로 가짜 진행률을 만들지 않는다.

구현 시 하나의 계약 테스트 묶음으로 아래를 실행한다. 아직 해당 테스트가 구현됐다는 뜻은 아니다.

| 시험 | 기대 결과 |
|---|---|
| 같은 key 동시 생성 2회 / 같은 key 다른 spec | operation·VM 각각 하나 / 충돌 409 |
| 다른 key·같은 ETag의 stop/delete / unresolved create 뒤 새 key | resource claim 하나만 획득 / 배정·슬롯 유지, 새 VM 생성 없음 |
| 다른 tenant의 ID·plan·operation 조회·승인 | provider 호출 없이 차단, 정보 누출 없음 |
| stale ETag / 만료 plan / unsupported volume | 412 / plan_expired / capability_unsupported, 외부 변경 없음 |
| dispatch 후 응답 유실 | provider readback; 확인 불가면 outcome_unknown, 자동 재생성 없음 |
| 202 수신, VM ACTIVE이나 bootstrap 실패 | 접수 표시만; CI·배포 시작 차단 |
| Terraform state 변경·잠금·부분 apply | 새 plan 또는 확인 대기; 자동 unlock/전체 destroy 없음 |
| 자원 2개 중 두 번째 실패 | 첫 자원 ID·비용·미완료 단계 보존; 공유 자원 삭제 없음 |
| workspace stop/start / compute 삭제 | 지원 profile에서 파일 보존; retained disk 인벤토리·비용 유지 |
| 활성 CI의 stop/delete / 사용 중 DB가 있는 앱 노드 삭제 | drain·저장·의존성 검사 전 provider 호출 없음 |
| 앱 workload 삭제 / 오래된 앱의 HTTP 200 | 인프라·DB 유지 / 새 배포 완료로 오판하지 않음 |

실행 기록에는 test ID, 코드 revision, target/profile hash, operation/resource ID, provider request ID, observed_at, expected/actual, PASS/FAIL/BLOCKED/NOT_RUN과 마스킹한 근거를 남긴다. 하위 Controller가 없는 상태의 모의 테스트와 실제 온프레·AWS 통과 기록을 구별한다.

## 8. 여러 CSP를 연결하는 컨벤션

### 두 종류의 어댑터와 의존 방향

**제품 provider adapter**는 우리 네 CRUD 계약을 구현하면서 인증된 target의 capability, 공급자 호출·오류·관측을 변환한다. **Terraform provider**는 Terraform 내부에서 공급자의 자원 API와 state를 다루는 플러그인이다. 둘을 하나로 취급하지 않는다. 제품 adapter는 target/resource kind에 따라 검토된 Terraform 모듈 또는 native API/기존 Controller 경로를 선택한다.

```text
콘솔·에이전트 → 인증된 target_id + 표준 desired spec
             → 제품 API/worker (계획·승인·operation, CSP SDK import 없음)
             → 등록된 provider adapter (AWS / OpenStack / 후속 CSP)
             → Terraform executor 또는 해당 native API/Controller
             → 표준 InfraResource + 비밀 없는 NodeDescriptor
             → 승인된 Ansible 구성 실행 → readiness → CI/Argo CD
```

AWS와 OpenStack은 첫 구현 대상이며 Azure/GCP 등은 확장 대상이다. Terraform provider가 존재한다는 사실만으로 우리 제품이 그 CSP를 지원한다고 표시하지 않는다. adapter를 선택하는 단순 등록표부터 구현하고 플러그인 자동 탐색·범용 dependency injection 프레임워크는 만들지 않는다.

| 항목 | 규칙 |
|---|---|
| `target_id` | `aws-seoul-poc`, `onprem-lab` 같은 관리자 등록 alias. 사용자 API는 이 값만 선택; 실제 계정·프로젝트·리전·네트워크는 서버가 검증 |
| `provider_kind` | `aws`, `openstack` 등 공급자 식별. 애플리케이션의 제품/모델 provider와 다른 필드 |
| `execution_driver` | `terraform`, `native_api`, `controller` 중 구현된 경로. 자원 생성 후 고정; CSP 이름과 혼합하지 않음 |
| `owner_ref` | 자원의 유일한 변경 권위: Terraform state 주소 또는 allocator/Controller 범위. 동적 VM과 Terraform의 이중 소유 금지 |
| provider ID | adapter가 해석하는 opaque reference. 공통 코드에서 `i-` prefix·ARN·Azure 경로·UUID 형식을 파싱하지 않음. target scope와 함께 저장 |
| spec·관측 | 공통 CPU/vCPU, 메모리 MiB, 디스크 GiB, architecture, desired power, 보존 요구를 사용. 공급자 원래 status는 내부 근거로 함께 보존 |
| 네트워크·스토리지 | `network_profile_ref`, `storage_profile_ref`, `ingress_profile_ref`, `secret_profile_ref`로 연결. VPC/subnet/SG/EBS/SSM 같은 이름은 공급자 매핑 안에만 배치 |
| 접속·인증 | `transport_ref`, `credential_binding_ref`는 서버가 등록한 참조. 기본적으로 짧은 수명 자격과 해당 작업 범위. 브라우저·에이전트·사용자 VM에 관리 자격 전달 금지 |
| schema·버전 | HTTP 계약 `/v1`, target 설정 `schema_version: v1`; profile revision·adapter revision·도구 lock을 계획 hash에 포함. 의미/단위 변경은 새 계약 버전으로 처리 |
| 이름·라벨 | 논리 ID는 불변, 표시 이름은 변경 가능. `project`, `environment`, `component`, `resource_id`, `managed_by` 메타데이터를 유지하고 공급자별 길이/문자 제한은 adapter가 안정적으로 변환. 태그 자체는 권한 증거가 아님 |

현재 AWS의 default VPC·첫 subnet·`stable/current` AMI·서울 리전을 공통 기본값으로 복사하지 않는다. 운영 target은 허용 계정/프로젝트·리전/zone·network/image 참조를 명시하며, 해당 공급자에서 조회한 실제 값과 architecture를 실행 전 검사한다. 예산 profile이 다른 CSP에서 의미하는 성능·가격은 별도 검증한다. `2 vCPU`가 같은 성능이나 같은 비용을 보장하지 않는다.

### capability와 설정 예시

지원 상태는 action/feature마다 `supported`, `unsupported`, `unverified`로 기록한다. 필수 capability가 unverified/unsupported면 실행을 막는다. 예: compute 생성·조회·start/stop/delete, 생성 요청 correlation/idempotency, stop 시 디스크 유지, 명시 volume 보존, snapshot/restore, 네트워크·IP 수명, 내부 접속, guest bootstrap. stop이 디스크 보존인지 RAM 유지인지 구분하고, 어느 CSP도 기본으로 hibernate 의미를 부여하지 않는다. 공인 IP·DNS 유지도 capability에 근거한다.

다음은 **관리자 설정 형식 예시**다. 참조된 profile은 아직 실행 가능한 파일이 아니며 로더/검증 구현은 후속 작업이다. 이 파일을 사용자 업로드에서 받지 않는다.

```yaml
schema_version: v1
targets:
  aws-seoul-poc:
    provider_kind: aws
    provider_config_ref: aws-seoul-poc-v1
    credential_binding_ref: aws-platform-operator
    state_backend_ref: platform-state-poc
    drivers:
      foundation: terraform
      workspace: native_api
    configuration_profile_ref: linux-ci-v1
    workload_profile_ref: k3s-budget-v1
  onprem-lab:
    provider_kind: openstack
    provider_config_ref: openstack-lab-v1
    credential_binding_ref: onprem-controller-client
    drivers:
      workspace: controller
    configuration_profile_ref: linux-ci-v1
    workload_profile_ref: k3s-onprem-v1
```

각 profile의 참조는 정확한 revision/hash로 해석해 계획에 저장한다. 같은 `linux-ci-v1`을 재사용하려면 OS/architecture/접속·저장소 요건을 실제로 통과해야 한다. 온프레의 `drivers.foundation`이 빠진 것은 지원하지 않는다는 의미이며 AWS로 자동 fallback하지 않는다. `state_backend_ref`는 제품 운영자의 state 저장 위치이므로 배포 CSP와 독립적이다. 다만 AWS에서 완전히 독립해야 하는 설치에서는 state backend도 그 환경에 맞게 이전해야 한다.

### 코드·모듈 배치와 경계

- 공통 DTO·검증·operation은 기존 API 내부의 infra 패키지에 둔다. 공급자별 파일에서만 해당 CSP SDK/Controller client를 import한다. 공통 worker나 UI의 `if provider == aws` 분기는 금지하고, 공급자 선택은 등록표와 target resolver에서 한 번 수행한다.
- Terraform은 `infra/terraform/<provider>/<stack>/` 형태로 새 모듈을 추가한다. 기존 `aws/`, `control/`은 현재 state 주소를 보존하며 재사용한다. 디렉터리 이름을 맞추려는 이유로 이동·재생성하지 않고, 필요한 이전은 별도 state migration으로 처리한다. cloud별 root module에 provider/backend 설정을 두며, 하나의 거대 모듈에 모든 CSP 리소스를 조건문으로 쌓지 않는다.
- Ansible 공통 OS·사용자·systemd·CI/desktop 설정에는 EC2/SSM/ARN을 넣지 않는다. 기존 playbook에서 공급자별 credential/transport task만 먼저 분리하고, 실제 재사용되는 작업을 task include/role로 추출한다. provider×host-role 조합마다 전체 playbook을 복제하지 않는다.
- `control`, `workspace`, `app_node`가 host role이다. SSM agent·SSM secret 조회는 AWS binding이며 OpenStack bootstrap에 강제하지 않는다. apt/snap은 OS 계열의 차이로 구분하고 AWS 기능으로 분류하지 않는다. 온프레 transport·secret 공급자는 등록·검증한 방식만 허용한다. 인터넷 공개 SSH를 공통 fallback으로 추가하지 않는다.
- `root-app.yaml.j2`의 현재 `clusters/aws/platform`과 AppSet의 `apps/aws/*/*`는 검증된 target별 GitOps 경로로 바꿀 구현 항목이다. 공통 GitOps base를 재사용하고 storage/secret/ingress 차이만 대상별 선언에 둔다. 같은 Kubernetes 객체를 여러 소유자가 적용하지 않는다.
- 모델 계정 배정은 CSP 어댑터의 책임이 아니다. 기존 계정 binding을 신뢰된 control runtime에만 연결하고 VM 이미지나 user-data에 로그인 값을 bake하지 않는다.

### CSP 전환과 적합성 시험

새 작업의 대상 선택은 `target_id` 교체로 가능하게 한다. **이미 존재하는 자원의 provider_kind/target을 덮어쓰는 것은 마이그레이션이 아니다.** 기존 resource ID와 owner를 유지하고 새 target에 별도 자원을 생성한다. 앱 이미지와 CI 계약은 재사용하되 아키텍처, 스토리지, DB, secret, ingress 요건을 다시 검사한다.

운영 앱 이전 순서는 새 대상 준비 → 데이터 복사/복원·secret 재바인딩 → 동일 artifact 검증 → 쓰기 정지/최종 데이터 동기화가 필요한지 판정 → 전환 Allow → 트래픽/DNS 전환 → 새 대상 확인 → 보존 기간 이후 이전 자원 정리다. 실행 중 job·승인 대기는 원래 target에 묶고, 앱 binding을 원자적으로 바꾸기 전까지 새 target으로 흘리지 않는다. RPO/RTO·정지 시간은 측정 전 수치를 보장하지 않는다. AWS native snapshot을 다른 CSP가 바로 복원할 수 있다고 가정하지 않는다.

새 CSP 출시 조건은 같은 계약 테스트의 실제 통과다: 정규화 단위/상태/오류, 권한 scope, 중복·응답 유실·동시 변경, unsupported 기능 거부, 동일 입력의 plan 안정성, VM 보존/재개, secret 마스킹, bootstrap readiness, 동일 stateless 앱 digest 배포와 데이터 이전·복구. 모의 adapter 통과와 실제 공급자 통과를 분리한다. 첫 이식성 검증은 AWS와 OpenStack에서 수행하며 추가 CSP의 빈 stub을 지원 목록에 넣지 않는다.

## 9. Terraform·Ansible의 제품 운영 계약

### 자원과 구성의 소유자

| 책임 | 쓰기 소유자 | 다른 도구와의 경계 |
|---|---|---|
| 계정 내 기반 네트워크·IAM·VM template·고정 앱/컨트롤 VM·명시 데이터 볼륨·state 기반 | 공급자별 Terraform module | 허용된 네트워크를 조회하거나 생성. Ansible의 cloud 생성 모듈·Terraform provisioner 셸 실행으로 같은 자원을 관리하지 않음 |
| 사용자 VM 배정·전원·유휴·삭제 | 제품 allocator + provider adapter | 고정 template/profile 사용. TF가 소유하는 template과 allocator VM의 owner를 분리 |
| OS·패키지·서비스 사용자·권한·마운트·systemd·CI/desktop/runtime | Ansible configuration job | 공급자가 내준 자원에 구성 적용. 볼륨 생성·삭제·할당 정책은 맡지 않으며, 데이터가 있는 장치를 자동 format하지 않음 |
| k3s·Cilium·Traefik bootstrap 설정·Argo 설치·root Application | Ansible가 파일 관리, k3s controller가 선언 적용 | 현재 bootstrap 소유 객체를 Argo에서도 중복 관리하지 않음. 향후 소유 이전 시 파일/객체 inventory와 prune 방지·readback 필요 |
| CNPG/secret controller·namespace 정책·앱 workload/DB 선언 | Argo CD와 해당 Kubernetes controller | 기존 GitOps 선언의 소유 경계 유지. Terraform helm/kubernetes provider·Ansible 앱 apply를 추가하지 않음 |
| 최초 기동 | cloud-init | 고정 bootstrap bundle·비밀 없는 binding 전달 및 최초 구성 시작. 상시 drift 조정자 아님 |
| 운영 마감·작업 중지 정책 | 제품 API/worker | Ansible은 승인된 timer/service 설정을 배치할 뿐 임의 마감 계산을 반복하지 않음 |

### Terraform: 검토 가능한 자원 변경

실행 단위는 `(environment, target_id, stack)`이며 platform control, shared foundation, app-cluster와 보존 데이터의 장애 영향 범위를 나눈다. 첫 단계는 기존 control/aws state를 유지하면서 원격화하고, 데이터 분리 시 `moved`/import 등 검토한 이전 계획으로 객체를 보존한다. 사용자별 동적 VM 때문에 state를 하나씩 만들지 않는다.

운영 executor는 사용자 CI와 분리하고 승인된 module commit·CLI/provider lock·변수 참조만 실행한다. `fmt/validate → plan → 정책·비용·파괴 영향 검증 → Allow → 정확한 saved plan apply → 실제 자원 readback`으로 처리한다. state/plan은 비밀을 포함할 수 있으므로 원문은 제한 저장하고 사용자에게는 요약만 제공한다. 요청자·승인자·executor의 권한을 분리하고, 관리자 단독 PoC의 자기 승인도 기록에 구별한다. HCP Terraform의 plan/정책/승인/apply run 구분을 제품 계약의 참고로 삼는다. [HCP run lifecycle](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/run/states).

공동 실행 전 원격 state의 암호화·버전 보존·접근 권한·locking·복구 시험을 필수로 한다. AWS 첫 구현은 S3 native lock을 선택하되 이를 지원하는 CLI 판본을 검증해 고정한다. 현재 적용한 1.5.7에 `use_lockfile`만 추가하지 않는다(S3 native lock은 1.10.0 이상). 신규 DynamoDB lock 구성은 추가하지 않는다. 다른 backend도 동일한 동시 실행 차단·복구 성질을 시험한다. state backend는 workload와 별도 소유해 일반 stack destroy에 포함하지 않는다. [S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3), [AWS 운영 지침](https://docs.aws.amazon.com/pdfs/prescriptive-guidance/latest/terraform-aws-provider-best-practices/terraform-aws-provider-best-practices.pdf).

plan 실패는 변경 없이 종료하고, apply 부분 실패는 실제 ID/state를 대조한 뒤 수리 계획을 만든다. Terraform에는 모든 인프라 변경을 안전하게 되돌리는 일반 rollback이 없으므로 자동 destroy를 복구로 사용하지 않는다. VM image 교체와 데이터 볼륨의 삭제/복원은 별도 승인이다. compute 교체 전에 영속 데이터가 root disk에 있는지 확인하고 분리·보존·백업을 먼저 처리한다.

### Ansible: 버전이 고정된 구성 작업

구성 실행은 네 CRUD를 늘리는 공개 임의 셸 API가 아니라 기존 worker의 별도 `operation.kind = configure_node`다. 상위 workflow에서 infra 생성 이후 또는 승인된 유지보수 시 실행한다. 입력은 `resource_id`, host role, **NodeDescriptor**, configuration revision, runtime lock, 허용 parameters hash와 credential binding이다. 임의 inventory·playbook URL·`extra_vars`·셸은 사용자/에이전트 입력에서 받지 않는다. 구성·인프라 변경은 §4의 같은 resource mutation claim을 사용해 resize/terminate 도중 Ansible이 실행되지 않게 한다. Terraform backend lock만으로 guest 구성 작업까지 직렬화됐다고 가정하지 않는다.

NodeDescriptor는 adapter가 확인한 provider ref·OS/image revision·architecture·연결 참조·마운트할 볼륨 ID·승인된 secret 참조·GitOps path를 담는다. Terraform output 전체나 state를 Ansible에 넘기지 않고 허용된 필드만 전달한다. template 적용 값은 검증하고 command/shell 문자열에 비신뢰 값을 보간하지 않는다.

Red Hat AAP의 job template처럼 **프로젝트 revision, playbook, inventory 범위, credential binding, 실행 환경**을 한 구성 작업에 고정한다. 첫 구현은 ansible-core와 기존 worker로 충분하며 AAP/AWX 설치는 운영자 수·노드 수·위임 요구가 생길 때 검토한다. 실행 환경은 Ansible/Python/collection·패키지 출처의 lock으로 재현하고, 제어용 실행 컨테이너를 쓰면 digest까지 기록한다. [AAP job template](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/get_started-con_gs_auto_dev_job_templates).

첫 부팅은 cloud-init이 검증된 immutable commit/bundle을 실행한다. 후속 재구성은 제품의 승인된 job이 등록 transport로 같은 bundle을 실행한다. AWS는 SSM 기반 로컬 실행 또는 검증된 pull 경로를 사용하며 온프레는 담당 Controller가 제공하는 인증된 실행 경로를 확인한다. 접속 경로가 없으면 blocked다. 재실행 경로를 구현하기 전 branch 최신 내용을 주기적으로 root 권한으로 당기는 timer를 켜지 않는다.

구성 변경은 `변수/schema 검증 → syntax → 가능한 범위의 check/diff → 영향/재시작 검토 → drain/유지보수 → 적용 → 버전·서비스·readiness 검사 → 결과 기록` 순서다. check mode 미지원 task는 `NOT_CHECKED`로 명시하며, simulation 통과를 실제 적용 성공으로 간주하지 않는다. secret task는 `no_log: true`와 `diff: false`로 보호하고 실행 로그/artifact의 비밀 유출을 시험한다. [Ansible check/diff의 한계](https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_checkmode.html).

반복 적용 시 원치 않는 restart·파일 변경이 없어야 한다. 비멱등 task는 이유·영향을 명시하고 `changed_when: false`로 실제 변경을 숨기지 않는다. 특히 k3s 업그레이드는 기존 `creates: k3s.service`만으로 처리하지 않는다. 대상 버전/설정 비교 → 단계적 바이너리 배치 → 승인된 restart/reboot → 실행 버전·node/controller Ready 검증을 별도 작업으로 구현한다. 버전 하향·DB format 변경은 안전한 역변환이 입증되지 않으면 이전 playbook 자동 재실행으로 되돌리지 않는다.

여러 노드에는 canary/순차 적용을 사용하고 건강 검사가 실패하면 다음 노드를 중단한다. 현재 단일 앱 노드는 무중단 rolling upgrade를 보장할 수 없으므로 유지보수 중단을 계획에 표시한다. cloud-init 단계의 비용 cutoff와 control.yml이 같은 systemd 파일을 쓰는 현재 예외는 하나의 deadline·revision으로 인계하도록 정리한다. 다운로드 실패 전에도 cutoff가 살아야 하므로 초기 보호 장치를 먼저 제거하지 않는다.

### 상태·drift·복구·검증

`infra_ready`, `configuration_ready`, `runtime_ready`를 별도로 기록한다. Ansible receipt에는 operation/resource/configuration revision, 실행 도구 lock, 대상 OS/architecture, 적용 전후 버전, task 실패·변경 요약, 재시작 여부, 검증 시각을 남긴다. 로컬 `bootstrap-ready.json` 하나만 신뢰하지 않고 관리 경로의 실제 서비스/runner/controller 상태와 대조한다. 비신뢰 workspace의 자기 보고만으로 플랫폼 보안 준비를 판정하지 않는다.

TF plan은 공급자 자원 drift를, Ansible audit/check와 실제 버전 검사는 guest 구성 drift를, Argo는 Kubernetes 선언 drift를 관측한다. 관측 결과는 같은 operation 이벤트로 올리되 자동 수정 권한은 각 소유자에만 둔다. 배포·재부팅·비용이 생기는 drift 수정은 정책/Allow를 거친다. 이 절은 drift 작업 설계이며 지금 주기 실행을 등록한 것은 아니다.

제품화 인수 기준은 (a) 새 노드 bootstrap, (b) 같은 구성 두 번째 적용의 무변경·무불필요 restart, (c) 승인된 버전 변경 후 실제 프로세스 버전 일치, (d) 부분 실패 후 유지된 데이터로 재개, (e) 동시 TF/configuration 작업 차단, (f) state lock·복구, (g) credential 실패/rotation의 명확한 판정과 비밀 미노출, (h) compute 제거와 데이터 보존, (i) AWS/OpenStack에서 같은 NodeDescriptor·구성 계약을 사용한 실제 검증이다. 각 결과는 PASS/FAIL/BLOCKED/NOT_RUN으로 남긴다.

## 10. 2026-10-01 공통 실행기 구현 상태

`platform/infra/provision.py`는 AWS/GCP/Azure native Terraform module을 고정 allowlist로 선택하고 private state·saved plan·검토 요약·hash 일치 확인·apply를 공통 처리한다. 사용법과 제한은 [인프라 실행 문서](../../infra/README.md)를 따른다. 이 관리자 CLI는 위 네 CRUD 전체의 구현이 아니다. 관리자 콘솔에는 영속 job/event·Allow kernel이 있지만 Terraform 계획과 승인 소비·작업 dispatch 연결, native VM lifecycle, remote state와 분산 작업 조정은 남아 있다. `capabilities.py`의 start/stop/delete는 현재 false이며 AWS apply는 billing importer 부재로 사전 차단한다. GCP 실자원 검증과 Azure 로컬 검증은 [검증 기록](../scenarios/cloud-validation.md)에 분리했다. **2026-10-01 20:54 KST 재확인:** 원래 AWS/CI 담당 범위인 Terraform·Argo bootstrap·CI/LKG 중 GCP bootstrap·재시작 readiness와 실제 CI는 검증했지만 AWS 새 앱 스택 E2E·CodeBuild·앱별 LKG는 남아 있다. [현재 통합 상태](../CONTROL-PLANE-PLAN.md#77-2026-10-01-구현-증분과-남은-검증)와 [CI 증거](../scenarios/ci-validation.md)를 따른다. 로컬 콘솔/채팅 개선이 기존 CD 작업의 중단을 뜻하지 않으며 별도 release 통합은 계속 진행 중이다.
