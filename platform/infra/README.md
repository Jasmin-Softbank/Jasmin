# 인프라 스펙시트와 비용 운영

이 디렉터리는 관리자용 로컬 Python CLI다. `specsheet.py`는 Terraform `node_descriptor`를 JSON·Markdown으로 정리하고, `costs.py`는 관리자가 가져온 청구 export를 SQLite에 저장해 범위별 사용 비용과 작업 예산 예약을 계산한다. 두 도구 모두 **클라우드를 조회하거나 변경하지 않는다.** 자동 청구 수집기, 가격 견적 API 연동, 제품 Allow/작업 실행기와의 예산 연동은 아직 없다.

`database.py`는 기존 DB 증거에 따른 옵션 검토 도구다. 실행·설치·migration을 하지 않으며, `test_database.py`가 별도 계약을 검증한다.

## 스펙시트

저장소 루트에서 실행한다. 아래 `/tmp/` 경로는 예시이며, 운영 자료는 관리자 전용 저장소에 둔다.

```bash
terraform -chdir=infra/terraform/gcp output -json node_descriptor > /tmp/node-descriptor.json
python3 platform/infra/specsheet.py /tmp/node-descriptor.json --format json --output /tmp/node-spec.json
python3 platform/infra/specsheet.py /tmp/node-descriptor.json --format markdown --output /tmp/node-spec.md
```

Azure도 해당 Terraform 디렉터리에서 같은 output을 사용한다. 전체 `terraform output -json`의 `node_descriptor.value`도 읽지만 다른 output은 복사하지 않는다. 현재 GCP/Azure의 서로 다른 storage·주소·bootstrap 필드를 공통 형식으로 정리한다. module이 제공한 machine type은 `configured`로 표시하며, vCPU·메모리가 descriptor에 없으면 `unknown`이다. 이름만 보고 머신 사양이나 가격을 추정하지 않는다. VM 개별 비용, 단가 견적, 전원 상태, mount·복구·실제 readiness도 자료가 없으면 확인된 것으로 표시하지 않는다.

현재 코드의 범위는 다음과 같다.

| 기능 | 현재 구현 | 이 스펙시트의 판정 |
|---|---|---|
| GCP/Azure VM 및 별도 데이터 디스크 | Terraform module + cloud-init | 구성 존재; 실제 동작은 `unverified` |
| 데이터 보존 | 별도 disk와 Terraform 삭제 방지, mount 선행 | 정책 존재; 복구 검증은 `unverified` |
| 앱 replica 자동 증감 | `platform/render/render.py`의 bounded KEDA ScaledObject·quota 렌더 | 구현 존재; 대상 cluster의 KEDA/Metrics Server·실제 동작은 별도 확인 |
| VM size 변경 | 관리자가 saved Terraform plan을 검토하는 유지보수 절차 | 자동 resize 없음 |
| 노드 수 자동 증감 | 미구현 | `not_implemented` |
| 실제 비용 집계 | 관리자 export를 로컬 import | 실시간 수집·자동 수집 미구현 |
| 작업 예산 hold | SQLite 트랜잭션과 operation ID로 중복·동시 예약 차단 | 클라우드 실행 권한 또는 Allow 승인이 아님 |

GCP 시험 target이 `max_run_duration_seconds = 7200`을 선택하면 스펙시트에 **7,200초 후 STOP 구성**이 표시된다. module 기본값은 `null`이며 모든 GCP VM에 2시간 제한이 있다고 가정하지 않는다. start마다 제한 시간이 다시 계산되므로 절대 예산 상한이 아니다. 중지 후 disk·예약 IP 비용은 남으며, 실제 STOP 관측과 작업 drain은 별도다.

## 비용 export와 범위

단가는 하드코딩하지 않는다. `reported_actual`은 공급자가 export한 금액을 정규화했다는 뜻이며, 실시간 계량·확정 청구서·최종 결제액을 의미하지 않는다. `period`는 **UTC 사용 날짜 기준 월**이다. invoice month와 다를 수 있다. 갱신은 같은 범위·공급자·월의 **전체 월 snapshot 교체**이며 delta append가 아니다. 빈 export는 비용 0의 증거가 아니므로 거부한다.

### GCP

관리자가 Cloud Billing의 **Detailed usage cost export**를 BigQuery에 먼저 구성해야 한다. 아래는 해당 프로젝트의 한 사용 월을 가져오는 SQL 예시다. 실제 billing dataset/table을 사용하고 `@target_project`, `@month_start`, `@next_month_start`를 명시적 query parameter로 제공한다. 이 저장소가 쿼리를 실행하거나 export를 활성화하지는 않는다.

```sql
SELECT
  usage_start_time,
  STRUCT(project.id AS id) AS project,
  STRUCT(resource.global_name AS global_name) AS resource,
  currency,
  CAST(cost AS STRING) AS cost,
  ARRAY(
    SELECT AS STRUCT CAST(credit.amount AS STRING) AS amount
    FROM UNNEST(credits) AS credit
  ) AS credits
FROM `BILLING_PROJECT.DATASET.gcp_billing_export_resource_v1_BILLING_ACCOUNT`
WHERE project.id = @target_project
  AND usage_start_time >= TIMESTAMP(@month_start)
  AND usage_start_time < TIMESTAMP(@next_month_start)
```

`cost + SUM(credits.amount)`를 청구 통화 그대로 합산한다. credits 부호를 반대로 바꾸거나 행을 사전 합산해 다시 더하지 않는다. `resource.global_name`이 없는 비용은 `UNALLOCATED`로 남긴다. VM만 필터링하면 retained disk·IP 등 비용이 빠지므로 프로젝트 snapshot을 보관하고 자원별 내역을 확인한다. 프로젝트 밖의 공유 비용·billing-account 수준 요금까지 이 합계가 포함한다고 가정하지 않는다. [Google 상세 export 스키마](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-tables/detailed-usage), [공식 쿼리 예제](https://docs.cloud.google.com/billing/docs/how-to/bq-examples).

전체 쿼리 결과를 **JSON 배열**로 저장한다. 콘솔 preview나 기본 row limit로 잘린 결과를 가져오지 말고, 같은 쿼리 결과의 row count와 파일의 행 수를 대조한다. BigQuery Cloud Storage export의 newline-delimited JSON은 그대로 입력할 수 없다. 여러 export shard를 빠짐없이 모은 뒤 관리자 환경에서 JSON 배열로 변환해야 한다. CSV·JSON 다운로드 및 조회 비용과 권한은 기존 billing 프로젝트 정책을 따른다. `--observed-at`은 원본 전체 snapshot을 실제로 확보한 UTC 시각이다. 오래된 파일의 시각만 갱신해서 freshness를 늘리지 않는다.

```bash
python3 platform/infra/costs.py --db /tmp/railshot-costs.sqlite import \
  --provider gcp --scope gcp/PROJECT_ID/2026-10 --source-scope PROJECT_ID \
  --period 2026-10 --observed-at 2026-10-01T04:00:00Z /tmp/gcp-usage.json
```

날짜와 프로젝트는 실제 export 값으로 바꾼다. importer가 모든 행의 `project.id`를 `--source-scope`와 비교한다. 다른 프로젝트가 섞이거나 scope 열이 없으면 거부한다. 이 검사는 행의 일관성 검사이며, 관리자가 빠뜨린 행이나 export의 진위를 자동 검증하지는 않는다.

### Azure

Cost Management에서 정확한 **subscription scope**, 월, **ActualCost** 데이터셋을 선택한다. AmortizedCost나 FOCUS export는 이 importer의 입력이 아니다. CSV가 여러 shard로 나뉘면 모든 파일을 한 헤더로 병합하고 행 수를 대조한다. 리소스가 사라졌더라도 해당 subscription의 disk·IP·reservation 구매 등 청구 행을 임의로 제외하지 않는다. 좁은 resource-group/tag 필터가 필요하면 별도 scope와 포함·제외 기준을 기록해야 하며, subscription 전체 비용으로 표시해서는 안 된다. [Azure export 절차](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-improved-exports).

`Date`, `SubscriptionId`, `ResourceId`, `CostInBillingCurrency`, `BillingCurrency` 또는 `BillingCurrencyCode`를 사용한다. EA/MCA의 대소문자 차이는 허용하지만 서로 다른 통화 헤더가 충돌하면 거부한다. Azure 이 필드의 금액은 세금·청구 credit 적용과 차이가 있을 수 있고, GCP의 credit 합산 결과와 같은 의미라고 가정하지 않는다. reservation·savings plan의 ActualCost 구매 시점과 amortization도 구별한다. [EA 스키마](https://learn.microsoft.com/en-us/azure/cost-management-billing/dataset-schema/cost-usage-details-ea), [MCA 스키마](https://learn.microsoft.com/en-us/azure/cost-management-billing/dataset-schema/cost-usage-details-mca).

```bash
python3 platform/infra/costs.py --db /tmp/railshot-costs.sqlite import \
  --provider azure --scope azure/SUBSCRIPTION_ID/2026-10 --source-scope SUBSCRIPTION_ID \
  --period 2026-10 --observed-at 2026-10-01T04:00:00Z /tmp/azure-actual.csv
```

모든 행의 `SubscriptionId`가 선택한 scope와 맞아야 한다. 어떤 청구 형식에서 이 열을 제공하지 않으면 임의 구독 값을 채우지 말고 지원되는 원본 export부터 준비한다.

## 보고·예산 예약

```bash
python3 platform/infra/costs.py --db /tmp/railshot-costs.sqlite report \
  --scope gcp/PROJECT_ID/2026-10 > /tmp/cost-report.json
python3 platform/infra/specsheet.py /tmp/node-descriptor.json \
  --cost-report /tmp/cost-report.json --cost-scope gcp/PROJECT_ID/2026-10 \
  --format markdown --output /tmp/node-spec-with-cost.md
```

스펙시트는 명시적으로 연결한 **billing scope 총액**을 보여 주며 VM 한 대의 원가로 붙이지 않는다. GCP project가 descriptor와 다르면 unknown이다. Azure는 운영자가 target과 선택한 subscription의 일치를 확인해야 한다. source scope가 확인되지 않거나 report가 없거나 합계가 비어 있으면 unknown이다. 관측 이후 기본 24시간이 지나면 stale로 표시한다. 통화를 섞어 더하거나 환율을 자동 적용하지 않는다.

예약 예시의 숫자는 사용자별 실제 견적을 넣어야 하는 **가상 입력**이며 클라우드 단가가 아니다.

```bash
python3 platform/infra/costs.py --db /tmp/railshot-costs.sqlite reserve \
  --scope gcp/PROJECT_ID/2026-10 --operation-id OPERATION_ID --currency USD \
  --incremental-cost 2.00 --limit 10.00 --unreported-cost 1.00
```

예약 판단은 `max(보고 비용, 0) + 기존 held 예산 + 새 작업 증가분 + 미보고 사용량 추정`을 사용한다. 증가분에는 compute 외 disk/IP/트래픽/예상 실행시간·보존 기간 등 해당 작업에 필요한 비용을 포함하고, 별도 검토 자료에 견적 시각·단가 출처·시간 범위·가정을 남긴다. 자료가 없으면 임의로 0을 넣어 통과시키지 않는다. 현재 월·확인된 source scope·fresh snapshot·같은 통화가 아니거나 예산을 넘으면 거부한다. 수집 지연 때문에 fresh snapshot에도 아직 청구되지 않은 사용량이 있을 수 있다.

예약은 자동 만료하지 않는다. 실제 provider 결과와 작업 종료·잔여 자원/비용을 확인한 관리자가 해제한다. 완료 여부가 불명확한 작업이나 여전히 비용이 발생하는 보존 자원을 근거 없이 해제하지 않는다.

```bash
python3 platform/infra/costs.py --db /tmp/railshot-costs.sqlite release --operation-id OPERATION_ID
```

held 금액과 이미 보고된 비용이 잠시 겹치면 보수적으로 중복 계산될 수 있다. 이 CLI는 원장의 hold만 관리하며 실제 클라우드 중지·청구 차단·사용자 권한·Allow 정책을 집행하지 않는다. 환율이 필요하면 적용 시각·출처·방향·반올림을 포함한 명시적 변환 절차가 별도로 필요하다. 현재 원장은 그 변환을 수행하지 않는다.

## 용량 변경 절차

앱의 CPU/메모리 기반 KEDA 증감은 기존 노드 안에서 replica를 바꾼다. `--enable-keda`는 관리자가 KEDA·Metrics Server readiness를 확인한 뒤 전달하는 신뢰 입력이고, bounded profile의 최대 replica와 quota를 넘기지 못한다. Pending Pod가 생겼다고 VM을 자동 생성하거나 증설하는 기능은 없다.

VM 크기를 바꾸려면 해당 provider의 지원 size·quota와 현재 사용량을 조회하고, 앱/DB/PVC·중단 시간·비용·데이터 보존 영향을 확인한다. 이어 `terraform plan -out` → 변경/삭제 영향 및 비용 검토 → 필요한 Allow → dispatch 차단 및 drain/backup → 승인한 saved plan apply → 실제 VM size·k3s·스토리지·Argo·앱 상태 확인 순으로 처리한다. 현재 GCP module은 `allow_stopping_for_update = false`라 실행 중 VM을 임의로 멈춰 resize하지 않는다. 정지·재시작/교체는 별도 유지보수 계획에서 명시한다. 단일 노드라 무중단 확장을 보장하지 않는다. [인프라 계약](../contract/infra-interface.md).

## 검증

Python 표준 라이브러리만 사용한다. 다음 검사는 클라우드 계정·CLI·자격·유료 모델 호출 없이 실행한다.

```bash
python3 -m unittest discover -s platform/infra -p 'test_*.py'
```

cost 검사는 credit·통화·공식 CSV 헤더·source scope·이전 월·stale·동시 예약·idempotency를 확인한다. 스펙시트 검사는 GCP/Azure output 차이, compute 제거 후 남은 disk, missing 값의 unknown 처리, 비용 범위·stale·JSON/Markdown CLI를 확인한다. 실제 billing export 수집, 청구서 대조, 가격 견적, VM 변경/중지, KEDA 동작을 이 테스트의 통과로 주장하지 않는다.
