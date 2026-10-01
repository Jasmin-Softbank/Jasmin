# 인프라 실행

## 공급자별 Terraform module을 실행하는 공통 CLI

`platform/infra/provision.py` 하나가 AWS/GCP/Azure의 `init → saved plan → JSON review → checksum 확인 → saved plan apply`를 실행한다. 공급자 선택은 코드 안의 `aws`, `gcp`, `azure` allowlist로만 하며 임의 repo/module 경로·셸 명령은 target 입력으로 받지 않는다. 각 cloud-native Terraform module과 guest bootstrap은 그대로 유지한다. 새로운 provider SDK·추상 클래스·factory는 없다.

이 CLI는 **신뢰된 관리자용**이며 제품 API·인증·Allow 서비스가 아니다. 관리자 target JSON은 사용자 업로드나 모델 출력에서 직접 받지 않는다. 계정·프로젝트·구독·리전과 실행 환경의 실제 로그인 주체는 운영자가 먼저 확인한다. CLI의 binding 검사는 검토한 구성의 일치 여부를 확인하며, 클라우드 계정 권한을 인증하지 않는다.

```json
{
  "schema_version": "v1",
  "provider_kind": "gcp",
  "target_id": "gcp-seoul-poc",
  "execution_driver": "terraform",
  "variables": {
    "target_id": "gcp-seoul-poc",
    "project_id": "REPLACE_PROJECT_ID",
    "region": "asia-northeast3",
    "zone": "asia-northeast3-a",
    "machine_type": "e2-standard-4"
  }
}
```

위 예시는 형식만 보여 준다. 선택한 module의 `.tfvars.example`/`variables.tf`를 보고 image·GitOps·bootstrap 등 **모든 필수 입력**을 `variables`에 채워야 한다. 변수는 공급자별 사전을 그대로 넘긴다. AWS `instance_type`, GCP `machine_type`, Azure `vm_size`를 vCPU나 메모리에서 자동 환산하지 않는다. GCP/Azure `variables.target_id`는 최상위 alias와 같아야 한다. AWS module은 아직 `target_id` 입력과 표준 `node_descriptor` output이 없으며, 이 CLI가 지원 동등성을 만들어 주지는 않는다.

```bash
python3 platform/infra/provision.py plan \
  --target /PRIVATE/admin-target.json --state-root /PRIVATE/railshot-state
```

`state-root`는 checkout 밖의 절대 경로로 지정한다. target마다 0700 디렉터리를 만들고 파일은 0600으로 보호한다. 고정 module의 `.tf`, `.tftpl`, provider lock만 private 작업 디렉터리에 복사하고, Terraform local backend의 state를 해당 target 디렉터리에 저장한다. 출력의 `owner_ref`는 그 정확한 state 경로다. target에 `owner_ref`를 추가할 때도 이 값과 일치해야 한다. 같은 target에 두 CLI가 동시에 들어오면 로컬 lock으로 거부한다. 공유 운영 환경의 원격 backend·분산 locking·암호화·state 복구는 아직 이 CLI에 구현하지 않았다.

성공한 plan은 `reviewed.tfplan`, `plan-manifest.json`, `review.json`을 남긴다. 표준 출력에는 resource address·action·delete/replace 위험과 plan SHA-256만 요약한다. 변수·raw plan·Terraform 로그에는 비밀이 들어갈 수 있으므로 private 파일(`plan.raw.json`, `terraform.log`, `module/inputs.tfvars.json`)을 Git·사용자 VM·브라우저에 공개하지 않는다. 자격은 관리자 실행 환경의 기존 인증 경로로 제공하고 target 변수에 복사하지 않는다.

관리자가 정확한 변경·중단·데이터 보존·비용과 필요한 drain/backup을 검토한 뒤, 출력된 SHA-256을 명시해 **그 saved plan만** 실행한다.

```bash
python3 platform/infra/provision.py apply \
  --target /PRIVATE/admin-target.json --state-root /PRIVATE/railshot-state \
  --plan-sha256 REVIEWED_64_HEX_SHA256
```

apply는 plan bytes, target/provider/owner binding, 변수, 원본 module 및 복사된 source/provider lock hash, Terraform CLI 판본을 다시 확인한다. 달라지면 새 plan과 검토가 필요하다. `--plan-sha256` 입력은 운영자의 명시적 실행 확인이며 제품 Allow 토큰이나 2인 승인을 대신하지 않는다. 적용 시도를 기록한 뒤 실패하거나 결과가 불명확하면 같은 plan을 자동 재실행하지 않는다. 실제 자원/state를 대조하고 새 plan을 검토한다. Terraform apply 완료도 guest·Kubernetes·앱 readiness 완료를 뜻하지 않는다.

**기존 state를 자동 복사하거나 합치지 않는다.** 현재 provider 디렉터리나 다른 executor가 이미 관리하는 자원을 이 CLI로 옮길 때는 별도 승인된 state 이전과 no-change plan 확인을 먼저 해야 한다. 빈 state로 같은 자원을 다시 생성하지 않는다. target별 source copy는 한 target의 최신 plan을 만들 때 교체되며, state는 별도 경로에서 유지한다. 기존 module의 remote backend를 이 CLI에 임의로 추가하지 말고 executor/backend 계약부터 수정한다.

검증은 다음 명령으로 수행한다. 테스트는 Terraform subprocess를 전부 mock하며 실제 cloud/init/plan/apply를 실행하지 않는다.

```bash
python3 -m unittest discover -s platform/infra -p test_provision.py
```

스펙시트·비용 집계·수동 용량 변경 절차는 [platform/infra/README.md](../platform/infra/README.md), ownership·승인·drain 계약은 [infra-interface.md](../platform/contract/infra-interface.md)를 따른다.
