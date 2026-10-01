# 클라우드·SDK 검증 기록 — 2026-10-01

이 기록은 실제 자원 관측과 로컬 코드 검사를 구분한다. 관리자용 PoC이며 사용자별 VM allocator, 제품 Allow API, 원격 데스크톱 스트리밍, 전체 CI/CD 서비스의 완료 기록이 아니다.

## 팀 프로젝트 준비 — 2026-10-01 23:20 KST

- AWS: 기존 관리자 IAM 인증으로 서울 리전의 `RAILSHOT` Resource Group과 `/railshot/railshot-developers` IAM 그룹을 생성하고 API로 재조회했다. Resource Group은 `Project=railshot` 태그로 분류하며 독립 계정·권한·청구 경계가 아니다. 팀 권한은 별도 IAM 정책에서 제한해야 한다.
- 5명 명단 중 관리자는 기존 계정을 유지하고, 나머지 4명의 `/railshot/` IAM 사용자·콘솔 login profile을 생성해 팀 그룹에 넣었다. 최초 비밀번호 변경 필수, access key 0개, 사용자 직접 정책 0개를 재조회했다. 초기 비밀번호와 개인별 안내는 관리자 로컬 비공개 파일에만 보존했다. 자동 메일·메신저 발송은 하지 않았다.
- AWS inline `railshot-team-access`는 본인 비밀번호·MFA 셀프서비스와 MFA 인증 후 지정된 서울 EC2의 시작·중지·재시작을 허용한다. EC2는 `Project=railshot`와 `AccessScope=railshot-team-dev`가 모두 필요하며 기존 control/CI/app VM에 팀 태그를 추가하지 않았다. 새 VM 생성·SSM·CI 실행·IAM/결제 관리는 포함하지 않는다. 따라서 AWS 전체 개발·배포 권한 구성이 끝난 상태는 아니다. MFA 등록용 `ListVirtualMFADevices`에는 계정 전체 장치 메타데이터 조회 예외가 있다.
- AWS Access Analyzer 정책 검증 findings 0개, 적용 정책 원문 readback 일치, IAM 시뮬레이터의 허용·거부 14개를 확인했다. 본인 비밀번호/MFA 허용, 다른 사용자·프로젝트·관리 VM 및 권한 상승 거부를 포함한다. 시뮬레이션은 팀원 실제 로그인·MFA 등록·자원 조작 E2E가 아니다.
- GCP: 기존 RAILSHOT 전용 프로젝트를 재사용해 표시 이름을 `RAILSHOT`으로 갱신했다. 현재 로그인 계정의 프로젝트 Owner 권한과 프로젝트 ACTIVE를 확인했다. 개인용 CLI에 프로젝트·서울 리전/존을 지정한 `railshot` configuration을 만들었으며 기존 default는 유지했다.
- GCP Owner를 보존하고 나머지 4명의 Google 계정에 프로젝트 IAM을 연결했다. `RAILSHOT Developer` custom role은 standalone VM/디스크 변경 22개 권한이며 Compute Viewer·OS Admin Login·Logs Viewer·Monitoring Viewer·Service Usage Consumer와 TCP22 조건의 IAP 역할을 함께 부여했다. 역할 정의 및 7개 바인딩을 API로 재조회해 비교했다. IAM/서비스 계정/결제/방화벽 관리 권한은 주지 않았다. VM 내부 sudo와 개별 VM metadata/tag 변경은 허용되므로 관리자 자격을 보관하는 control VM을 이 팀 프로젝트에 섞지 않는다.
- GCP billing 연결은 `FAILED_PRECONDITION / QuotaFailure / Cloud billing quota exceeded`로 거부됐고 후속 조회에서도 `billingEnabled=false`다. 결제 연결 가능한 프로젝트 할당량 문제이며 IAM Owner 여부와 별개다. 기존 다른 프로젝트의 billing 연결 해제·자원 이전·추가 compute 생성은 수행하지 않았다.
- 남은 인수 확인: 각 팀원의 실제 로그인과 AWS MFA 등록, 팀용 자원의 접근·거부 시험. GCP는 billing 연결 이후 관리자가 사설 네트워크·egress·필요한 IAP 경로를 준비해야 하며, 팀 VM에는 서비스 계정·공개 IP를 부여하지 않는 범위다. IAP IAM 역할만으로 SSH/네트워크가 구성되지는 않는다.
- AWS root 로그인으로 전환하거나 root key를 생성하지 않았다. 현재 관리자 IAM 권한을 사용했다. GCP는 Google 계정과 프로젝트 IAM 권한으로 관리한다. 계정 식별자·이메일·private readback은 공개 Git에 넣지 않는다.

## 공통 실행 구조

| 공통 코드 | 공급자별로 남긴 부분 | 현재 검증 |
|---|---|---|
| `platform/runner/run_agent.py`: 역할 지침, 출력 schema, 경로 정책, 패치 적용, 실행 기록 | Codex SDK / Claude Agent SDK의 세션·인증·응답 API | Codex 실제 구독 인증 JSON 응답. Claude는 로컬 계약 검사 |
| `platform/loop/loop.py`, `gate/`: baseline, gate 순서, 실패 분류, bounded repair, 전체 재검사 | 없음. SDK 선택은 runner에 전달 | 오프라인 회귀 검사와 별도 GCP CI의 실제 fixture full gate. 범위는 [CI 검증 기록](ci-validation.md) 참조 |
| `platform/infra/provision.py`: private state, init, saved plan, 변경 요약, plan hash 확인, apply | AWS/GCP/Azure의 기존 native Terraform module | subprocess mock 검사. 이 executor로 기존 live state를 이전하지 않음 |
| `infra/ansible/node.yml`: guest 구성, 버전·ready 검사, Argo root | AWS의 SSM 등 실제 공급자 전용 작업 | GCP 실제 bootstrap. 공급자마다 별도 Ansible 복제본을 만들지 않음 |
| `ci/railshot-deploy.yml`: 검사 이미지 보존 → publish → GitOps → Argo 관측 | 등록한 `GITOPS_CLUSTER`, `STORAGE_CLASS`, Argo endpoint | 로컬 workflow template. 등록·원격 workflow 실행 미수행 |

클라우드 출력·청구 파일의 정규화는 `platform/infra/specsheet.py`, `costs.py`에 있다. 추정 비용, 관측한 자원 스펙, 실제 청구 금액을 분리하며 미수집 비용을 0으로 표시하지 않는다. AWS native module의 표준 NodeDescriptor 출력 및 공통 lifecycle 서비스는 후속 통합 대상이다.

## GCP 실제 실행

별도 테스트 프로젝트는 생성했지만 billing 연결이 프로젝트 할당량으로 거부되었다. 그 프로젝트에는 compute 자원을 만들지 않았다. 기존 관리자 소유의 billing 연결 프로젝트에서 전용 VPC·subnet·VM·service account로 시험했다. 기존 VM이나 다른 프로젝트의 자원은 변경하지 않았다.

| 항목 | 관측 결과 |
|---|---|
| Compute | 서울 `asia-northeast3-a`, `e2-standard-4`, API 확인 4 vCPU / 16 GiB |
| OS / 디스크 | Ubuntu 24.04, boot 30 GiB + 독립 pd-balanced 20 GiB |
| 데이터 | `/var/lib/rancher`를 독립 ext4 디스크에 mount. data auto-delete 비활성 및 Terraform 삭제 보호 |
| 접근 | OS Login + IAP SSH, 공용 SSH/HTTP/HTTPS ingress 없음. VM service account IAM 역할·API scope 미부여 |
| 수명 제한 | GCP native maxRunDuration 7,200초, terminationAction STOP. 설정을 확인했으며 2시간 만료 자체를 기다려 시험하지 않음 |
| Bootstrap | cloud-init 오류 없음, Ansible failed=0, k3s `v1.36.4+k3s1` Ready |
| 구성 | Cilium `1.20.2`, Argo Helm chart `10.9.4`, KEDA `2.21.0` |
| GitOps | root와 KEDA Application `Synced / Healthy` |
| 부하 | 격리된 CPU fixture에서 KEDA/HPA가 파드 1→3으로 확장. 부하를 제거한 후 3→1로 축소 |
| 중지·재개 | 실제 stop → start 후 독립 디스크 sentinel 일치, 동일 node Ready, KEDA rollout 정상, root/KEDA Synced·Healthy |

GCP root가 읽는 KEDA 선언은 실제 GitOps 저장소에 게시했다: [commit `35cedbde`](https://github.com/Jasmin-Softbank/railshot-gitops/commit/35cedbdeb8dbbf51033278bdf44434735e1e6018), 경로 `clusters/gcp/platform/15-keda.yaml`. 기존 AWS 선언 경로는 변경하지 않았다. 로컬 Jasmin 변경 전체를 원격에 게시했다는 의미는 아니다.

부하 fixture는 삭제했다. 테스트 후 VM은 중지하고 디스크와 예약 IP는 보존한다. 정확한 최신 상태는 관리자 CLI에서 다시 읽어야 한다. 중지된 앱 VM은 앱을 서빙하지 않는다. 이 시험은 제품 UI의 resume, 사용자 앱의 전체 Argo ApplicationSet 배포, DB migration·백업·복원을 검증한 것이 아니다.

별도 CI 전용 `railshot-ci-e2e`는 정상·실패 fixture 검증 후 증거를 로컬로 옮기고 삭제했다. 2026-10-01 17:06 KST API 재조회에서 해당 VM과 60 GiB boot disk가 없고, 원래 앱 VM `railshot-gcp-poc`는 `TERMINATED`임을 확인했다. 앱 데이터 디스크와 AWS control은 이 정리 대상이 아니다. private receipt: `~/.local/state/railshot/ci-e2e/ci-cleanup-receipt.json`.

Google Cloud Billing Catalog의 서울 E2 KRW 공개 단가 조회에서는 CPU·메모리 합계 약 **₩238/시간**이었다. 50 GiB pd-balanced의 월 환산은 약 **₩8,991/월**이며, 중지해도 보존 디스크와 예약 IP는 비용이 남는다. IP·네트워크·세금은 위 계산에 포함하지 않았고 실제 청구 export는 아직 연결하지 않았다. 이는 실제 사용액이 아닌 조회 당시 단가 추정이다. [Google Cloud Billing Catalog API](https://cloud.google.com/billing/v1/how-tos/catalog-api)

## AWS·Azure·SDK 경계

- 기존 AWS control VM에서 Python `openai-codex==0.159.3`의 실제 관리자 구독 인증 호출을 확인했다. 이후 `gpt-6.1-sol`의 실제 source repair·packaging adapter를 호출하고 별도 GCP CI에서 각 결과의 전체 gate PASS를 확인했다. 운영자가 제품 CLI를 연결한 구성 요소 검증이며 제품 자동 dispatch는 미구현이다. [CI 검증 기록](ci-validation.md) 참조.
- Claude Agent SDK 경로는 공통 runner의 출력·patch 정책을 사용한다. 실제 Claude 제공자 호출은 수행하지 않았다.
- Azure는 native Terraform 구성, 독립 data disk 보호, bootstrap template과 표준 출력이 있으며 fmt/validate 및 template 검사를 통과했다. Azure 계정·자원을 연결하거나 생성하지 않았다.
- GCP의 실제 apply는 기존 GCP module/state에서 수행했다. 이후 추가한 공통 executor가 이 자원의 소유권을 자동 인수하지 않는다. 이전 시 state 보존·이전과 no-change plan 확인이 필요하다.

## 다음 인수 순서

1. 정상 10종 full gate와 negative 49건을 별도 GCP CI에서 검증했다. 다음은 미지원 workspace·multi-module과 새 lock/test 자동 준비이며 정확한 범위는 [CI 검증 기록](ci-validation.md)을 따른다.
2. Codex 실제 Q 실패 → 제한 patch → 전체 gate PASS와 테스트 registry의 같은 image ID publish를 검증했다. Claude는 오프라인 계약만 확인했고 호출은 중단했다. 제품 자동 연결·production registry 인수는 남아 있다.
3. GitHub release 환경의 required reviewer와 제품 Allow를 연결한다. workflow의 `environment` 이름이나 CLI flag만으로 승인이 완성된 것은 아니다.
4. 등록한 cluster alias의 GitOps 경로·StorageClass·Argo ApplicationSet을 확인한 뒤 정확한 revision·image digest·health·URL을 검증한다.
5. DB 역할·migration 실패·데이터 보존·복원, autoscaling 중 Argo replica 소유권 및 용량/예산 한도를 실제 사용자 앱으로 검증한다.
6. 기존 state의 공통 executor 이전, provider lifecycle API, billing 수집, workspace allocator·제어권 lease·UI 연결을 통합한다. 실행 중 사용자 서비스가 있는 VM은 이 PoC의 자동 STOP 정책을 그대로 적용하지 않는다.
